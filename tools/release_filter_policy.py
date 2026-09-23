"""Canonical Orion release-package file filtering policy.

Both the packager and the independent security audit import this module. Keeping
the predicates here prevents one release gate from silently drifting from the
other.
"""

from __future__ import annotations

import re
from pathlib import Path


# All entries are lowercase because callers normalize path components before
# comparison. These may name either a directory or a file anywhere in a package.
FORBIDDEN_PATH_COMPONENTS = frozenset(
    {
        ".aws",
        ".env",
        ".netrc",
        ".pytest_cache",
        ".secrets",
        ".ssh",
        ".tokensave",
        ".vault",
        "__pycache__",
        "auth_tokens.json",
        "codesigning",
        "credentials.json",
        "delay_test.py",
        "get-psn-accountid.bat",
        "get_psn_account_id.py",
        "id_ed25519",
        "id_rsa",
        "learning.json",
        "license_cache.enc",
        "logs",
        "master.key",
        "nexus_svc.log",
        "orion-dev-key.local.ps1",
        "owner_strings.txt",
        # [ORION_PILL_REMOVED 2026-09-21, re-withdrawn 2026-09-23 owner] Pill (beta) is
        # withdrawn; its style profile must not ship. meter_detector.py tolerates a missing
        # style file. LOWERCASE: the matcher lowercases every path component first.
        "pill.json",
        "qmltooling",
        "redteam",
        "redteam_sandbox",
        "settings.json",
        "settings.json.sig",
        "starzen re",
        "adaptive_delay_plan.md",
    }
)


FORBIDDEN_FILE_SUFFIXES = frozenset(
    {
        ".backup",
        ".bak",
        ".db",
        ".dmp",
        ".exp",
        ".ilk",
        ".iobj",
        ".ipdb",
        ".key",
        ".lib",
        ".local",
        ".log",
        ".old",
        ".orig",
        ".p12",
        ".pfx",
        ".pdb",
        ".ppk",
        ".pyc",
        ".pyo",
        ".sqlite",
        ".sqlite3",
        ".tmp",
    }
)


FORBIDDEN_NAME_SUFFIXES = (
    ".local.ps1",
    ".local.bat",
    ".local.json",
    ".local",
)


# The production sidecar embeds these detector/timing modules. They are the
# application's high-value implementation and must never be recoverable as
# loose Python from a customer package. Thin launch wrappers are intentionally
# absent because the current package still ships those separately.
CROWN_JEWEL_PY_NAMES = frozenset(
    {
        "autogreen_sidecar.py",
        "compressed_meter_reader.py",
        "controller_remap.py",
        "decoder_pipe_identity.py",
        "luma_meter.py",
        "meter_detector.py",
        "meter_detector_yolo.py",
        "pose_timing.py",
        "remote_play_orchestrator.py",
        "simple_meter_reader.py",
        "tip_registration_infer.py",
    }
)


# Autonomous timing loads two reviewed JSON models and one learned ONNX meter
# locator from the source-bound OrionSidecar bundle. Any other models/ file is
# experimental/development baggage. Paths are relative to models/ and compared
# case-insensitively because the production filesystem is Windows.
APPROVED_MODEL_FILES = frozenset(
    {
        "latency_factory_prior.json",
        "orion_meter_detector.onnx",
        "tip_registration.json",
    }
)


# A standalone Nuitka bundle is required in production. The executable alone
# is insufficient: its source-binding record is part of the release evidence.
COMPILED_SIDECAR_REQUIRED_FILES = frozenset(
    {
        "OrionSidecar.exe",
        "ORION_SIDECAR_BUILD.json",
    }
)


# The inbound-meter-delay bridge is a shipped, customer-facing feature. Its elevated
# host and the WinDivert driver it loads must be present in a published package or the
# meter delay silently cannot engage on the install. Paths are package-relative POSIX
# (the bundle ships under packet_bridge/, see package_orion_release.PACKET_BRIDGE_SUBDIR).
# WAVE 3 (2026-08-08): the host is the C++ VeniceNetSvc.exe (native_orion/
# venicenet_service, wave 2A) — the Nuitka NexusVisionSvc.exe bundle no longer ships.
# The installer registers it under the customer-facing service name VeniceNetSvc
# (installer/orion.iss RegisterPacketBridgeService).
COMPILED_SERVICE_REQUIRED_FILES = frozenset(
    {
        "packet_bridge/VeniceNetSvc.exe",
        "packet_bridge/WinDivert64.dll",
        "packet_bridge/WinDivert64.sys",
    }
)


# ── shipped security policy: the FULL semantic contract ─────────────────────────
# tools/package_orion_release.write_security_policy() emits exactly these values and
# SecurityCore reads them at startup. A signature only proves the bytes are ours; it
# says nothing about whether the values fail closed. Every release gate that inspects
# a package validates the policy against this table, so a correctly signed package
# carrying `require_release_manifest: false` (or a dev bypass) is still refused.
SECURITY_POLICY_SCHEMA = "orion.security_policy.v1"
SECURITY_POLICY_REQUIRED_VALUES = {
    "require_release_manifest": True,
    "lock_automation_on_integrity_failure": True,
    "lock_automation_on_debugger": True,
    "lock_automation_on_analysis_tool": True,
    "allow_local_dev_bypass": False,
}


def security_policy_violations(policy: object) -> list[str]:
    """Return every way *policy* (already-parsed JSON) fails the production contract."""

    problems: list[str] = []
    if not isinstance(policy, dict):
        return ["security_policy.json is not a JSON object"]
    schema = policy.get("schema")
    if schema != SECURITY_POLICY_SCHEMA:
        problems.append(f"unexpected policy schema {schema!r}")
    for key, expected in SECURITY_POLICY_REQUIRED_VALUES.items():
        actual = policy.get(key)
        if actual is not expected:
            problems.append(f"{key} must be {str(expected).lower()} (got {actual!r})")
    return problems


# ── executable release allowlist (release blocker: no lab/test/stray exe ships) ──
# The copy phase must never admit an arbitrary top-level *.exe. Before this list,
# should_copy_top_file() shipped every .exe and only *Tests.exe was filtered, so a
# lab/check binary such as GreenWindowMathChecks.exe (does NOT end in tests.exe) and
# any stray *.packed.exe leaked into the package. This is the EXACT set of top-level
# executables a release package may contain; every other .exe at the package/build
# root is dropped by the copy filter and independently flagged by the audit.
#
# Subdirectory executables are intentionally NOT listed here — they are staged by
# dedicated allow-list copiers to fixed relative paths (OrionStream.exe under
# chiaki-ng-orion/, VeniceNetSvc.exe under packet_bridge/) and the audit reasons
# about those by the denylist below, never by this top-level positive set.
RELEASE_ADMITTED_EXECUTABLES = frozenset(
    {
        "OrionNative.exe",
        "OrionOwner.exe",
        "OrionStaff.exe",
        "OrionUpdater.exe",
        "OrionSidecar.exe",  # standalone Nuitka sidecar, staged from its .dist
    }
)


# The server-shard chain adds the unpacked activation broker and the packed inner
# payload at the package root (the Lethe bootstrap is staged AS OrionNative.exe, so it
# reuses that admitted name). These are admitted ONLY when packaging a server-shard
# release.
SERVER_SHARD_ADMITTED_EXECUTABLES = frozenset(
    {
        "OrionActivate.exe",
    }
)


# Executables that ship in a FIXED subdirectory. They are staged by dedicated
# allow-list copiers, never by the flat top-level filter, so the audit reasons about
# them by exact package-relative path. Anything else nested in the package is stray.
RELEASE_ADMITTED_NESTED_EXECUTABLES = frozenset(
    {
        "chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe",
        "packet_bridge/VeniceNetSvc.exe",
    }
)


# The ONE *.packed.exe name a package may ever contain, and only in a server-shard
# release: the Lethe-packed inner payload. Every other *.packed.exe is stray packer
# debris and is rejected everywhere (copy filter, scan, audit).
SERVER_SHARD_PACKED_PAYLOAD = "orionnative.packed.exe"


def is_lab_check_executable(name: str) -> bool:
    """Return whether *name* is a lab/check test binary that must never ship.

    Mirrors the ``*tests.exe`` rule for the ``*checks.exe`` convention used by
    standalone math/behaviour check targets (e.g. GreenWindowMathChecks.exe),
    which do not end in ``tests.exe`` and previously slipped past the filter.
    """

    return name.lower().endswith("checks.exe")


def is_stray_packed_executable(name: str) -> bool:
    """Return whether *name* is a packer-output ``*.packed.exe`` that is NOT the one
    permitted server-shard inner payload (``OrionNative.packed.exe``)."""

    lower = name.lower()
    return lower.endswith(".packed.exe") and lower != SERVER_SHARD_PACKED_PAYLOAD


def is_release_admitted_executable(name: str, *, server_shard: bool = False) -> bool:
    """Return whether a TOP-LEVEL *.exe is on the per-profile release allowlist.

    The positive allowlist governs the copy phase for the flat build-dir root. Non-exe
    names return False (they are governed by other predicates). In server-shard mode the
    broker is additionally admitted; the packed payload is produced by the packer into the
    package after copy, so it is governed by is_stray_packed_executable, not this list.
    """

    lower = name.lower()
    if not lower.endswith(".exe"):
        return False
    admitted = {n.lower() for n in RELEASE_ADMITTED_EXECUTABLES}
    if server_shard:
        admitted |= {n.lower() for n in SERVER_SHARD_ADMITTED_EXECUTABLES}
    return lower in admitted


def is_admitted_package_executable(relative_path: "str | Path", *,
                                   server_shard: bool = False) -> bool:
    """Profile-aware POSITIVE admission for an executable at a package-relative path.

    This is the audit-side twin of the copy filter: the copy phase drops an arbitrary
    top-level ``*.exe``, and this predicate independently rejects the same set (plus
    anything nested outside the fixed staged paths) in a package that was signed or
    assembled elsewhere. Case-insensitive because the production filesystem is Windows.

    ``server_shard`` admits exactly the two extra chain members: the unpacked broker
    ``OrionActivate.exe`` and the packed inner payload ``OrionNative.packed.exe``. In
    any other profile a ``*.packed.exe`` is packer debris and is refused.
    """

    rel = Path(str(relative_path)).as_posix().lstrip("./")
    lower = rel.lower()
    if not lower.endswith(".exe"):
        return True  # governed by other predicates; not an executable-admission question
    if "/" in lower:
        admitted_nested = {n.lower() for n in RELEASE_ADMITTED_NESTED_EXECUTABLES}
        return lower in admitted_nested
    if lower == SERVER_SHARD_PACKED_PAYLOAD:
        # The Lethe-packed inner payload is a SHARD-ONLY artifact. In any other
        # profile it is stray packer output at the package root.
        return bool(server_shard)
    return is_release_admitted_executable(Path(lower).name, server_shard=server_shard)


def is_stale_binary_name(name: str) -> bool:
    """Return whether *name* is an executable/DLL derivative or backup."""

    lower = name.lower()
    if lower.endswith(".exe.manifest"):
        return False
    if ".exe." in lower or ".dll." in lower:
        return True
    return re.search(r"(?:^|[._-])(?:bak|backup)(?:$|[._-])", lower) is not None


_OPENCV_DEBUG_DLL_RE = re.compile(r"^opencv_.*\dd\.dll$")

# Developer test transcripts occasionally land beside a runtime tree after a
# focused QtTest run. They are not runtime inputs and must not hitch a ride in
# recursive Chiaki/sidecar copies. Keep this name-based rather than banning all
# .txt files: third-party runtimes may legitimately carry license notices.
_TRANSIENT_TEST_TRANSCRIPT_RE = re.compile(
    r"^(?:focused_[a-z0-9_.-]+|full_tempo[a-z0-9_.-]*|"
    r"tempo_focused[a-z0-9_.-]*|test_output[a-z0-9_.-]*)\.txt$"
)


def is_debug_dll(path: Path) -> bool:
    """Identify actual MSVC debug DLL twins without name-only false positives."""

    lower = path.name.lower()
    if not lower.endswith("d.dll"):
        return False
    if _OPENCV_DEBUG_DLL_RE.match(lower):
        return True
    release_sibling = path.name[: -len("d.dll")] + ".dll"
    return (path.parent / release_sibling).exists()


def is_forbidden_file_name(name: str) -> bool:
    """Apply the canonical per-file release exclusions to *name*."""

    lower = name.lower()
    if lower in FORBIDDEN_PATH_COMPONENTS:
        return True
    if Path(lower).suffix in FORBIDDEN_FILE_SUFFIXES:
        return True
    if any(lower.endswith(suffix) for suffix in FORBIDDEN_NAME_SUFFIXES):
        return True
    if _TRANSIENT_TEST_TRANSCRIPT_RE.fullmatch(lower):
        return True
    if lower.endswith("tests.exe"):
        return True
    if is_lab_check_executable(lower):
        # Lab/behaviour check binaries (GreenWindowMathChecks.exe, ...) — release blocker.
        return True
    if is_stray_packed_executable(lower):
        # Any *.packed.exe other than the one server-shard payload is packer debris.
        return True
    return is_stale_binary_name(lower)


def is_crown_jewel_python(path: Path) -> bool:
    """Return whether *path* exposes an embedded production algorithm."""

    return path.name.lower() in CROWN_JEWEL_PY_NAMES


def is_unapproved_model_path(relative_path: Path) -> bool:
    """Return whether a package-relative path is undeclared models/ content."""

    parts = relative_path.parts
    if len(parts) < 2 or parts[0].lower() != "models":
        return False
    model_name = Path(*parts[1:]).as_posix().lower()
    approved = {name.lower() for name in APPROVED_MODEL_FILES}
    return model_name not in approved


from pathlib import Path  # noqa: E402  (the module is annotation-only above this line)

# == [ORION_QT_STYLE_PRUNE 2026-09-21] Qt Quick Controls style topology ==========================
# windeployqt copies EVERY Qt Quick Controls style because the style is chosen at runtime. Each
# packaged app pins exactly one -- OrionNative/Owner/Staff call QQuickStyle::setStyle("Basic")
# (native_orion/src/main.cpp, admin_tool_main.cpp), OrionStream pins Style=Material in its
# compiled-in qtquickcontrols2.conf -- so every other style is dead weight the packager prunes
# and the independent audit must refuse. ONE policy for both, so they can never disagree.
QT_QUICK_STREAM_DIR = "chiaki-ng-orion/chiaki-ng-Win"
QT_QUICK_STYLE_ROOTS: tuple[tuple[str, frozenset[str]], ...] = (
    # (app root relative to the package, styles that app may load)
    (".", frozenset({"Basic"})),
    (QT_QUICK_STREAM_DIR, frozenset({"Basic", "Material"})),   # Basic is Qt's fallback style
)
QT_QUICK_STYLE_SHARED_DIRS = frozenset({"impl"})       # helpers every style shares
QT_QUICK_NATIVE_STYLE_CONSUMER = "Windows"             # the only style importing QtQuick.NativeStyle
QT_QUICK_KNOWN_STYLES = frozenset(
    {"Basic", "Fusion", "Imagine", "Material", "Universal", "Windows", "FluentWinUI3"})
_QT_QUICK_CORE_FILES = (
    "qml/QtQuick/Controls/qmldir",
    "qml/QtQuick/Controls/qtquickcontrols2plugin.dll",
    "qml/QtQuick/Controls/impl/qmldir",
    "qml/QtQuick/Controls/impl/qtquickcontrols2implplugin.dll",
    "Qt6QuickControls2.dll",
    "Qt6QuickControls2Impl.dll",
)


def qt_quick_style_files(style: str) -> tuple[str, ...]:
    """The files a PINNED style needs to load, relative to its app root."""
    return (
        f"qml/QtQuick/Controls/{style}/qmldir",
        f"qml/QtQuick/Controls/{style}/qtquickcontrols2{style.lower()}styleplugin.dll",
        f"Qt6QuickControls2{style}StyleImpl.dll",
    )


def qt_quick_style_requirements_missing(app_root: Path, keep: frozenset[str]) -> list[str]:
    """Relative paths a pinned style needs that are ABSENT under `app_root`.

    A style directory that exists but lacks its qmldir, its QML plugin or its paired
    StyleImpl DLL renders an EMPTY window, not a crash, so presence of the directory alone is
    not a fail-closed check.
    """
    missing = [rel for rel in _QT_QUICK_CORE_FILES if not (app_root / rel).is_file()]
    for style in sorted(keep):
        missing += [rel for rel in qt_quick_style_files(style) if not (app_root / rel).is_file()]
    return missing


def qt_quick_style_allowed_root_dlls(keep: frozenset[str]) -> frozenset[str]:
    allowed = {"Qt6QuickControls2.dll", "Qt6QuickControls2Impl.dll"}
    for style in keep:
        allowed.add(f"Qt6QuickControls2{style}.dll")
        allowed.add(f"Qt6QuickControls2{style}StyleImpl.dll")
    return frozenset(allowed)


def qt_quick_style_violations(package_dir: Path) -> list[str]:
    """Qt Quick style content a shipped package must NOT carry (paths relative to the package).

    Used by the packager as its own post-prune self-check and by security_audit.py as an
    independent gate: a Controls style directory outside the allowlist, a Qt6QuickControls2
    DLL for a style the app cannot select, a `+<Style>` file-selector folder for such a style
    (Qt Quick Dialogs ships one per style) and QtQuick/NativeStyle without the Windows style.
    """
    out: set[str] = set()
    for rel_root, keep in QT_QUICK_STYLE_ROOTS:
        app_root = package_dir / rel_root
        qml = app_root / "qml"
        controls = qml / "QtQuick" / "Controls"
        if not controls.is_dir():
            continue
        allowed_dirs = keep | QT_QUICK_STYLE_SHARED_DIRS
        for entry in controls.iterdir():
            if entry.is_dir() and entry.name not in allowed_dirs:
                out.add(entry.relative_to(package_dir).as_posix())
        allowed_dlls = qt_quick_style_allowed_root_dlls(keep)
        for dll in app_root.glob("Qt6QuickControls2*.dll"):
            if dll.name not in allowed_dlls:
                out.add(dll.relative_to(package_dir).as_posix())
        for style in QT_QUICK_KNOWN_STYLES - keep:
            for selector in qml.rglob("+" + style):
                if selector.is_dir():
                    out.add(selector.relative_to(package_dir).as_posix())
        native = qml / "QtQuick" / "NativeStyle"
        if QT_QUICK_NATIVE_STYLE_CONSUMER not in keep and native.is_dir():
            out.add(native.relative_to(package_dir).as_posix())
    return sorted(out)
