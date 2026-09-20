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
