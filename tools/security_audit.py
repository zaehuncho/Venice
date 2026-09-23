#!/usr/bin/env python3
"""Non-invasive Orion release security audit.

This script is intentionally a gate, not runtime protection. It checks for
high-confidence release blockers that should be caught before a customer package
is shipped: concrete local dev keys, private key material, forbidden runtime
artifacts, missing package policy files, and manifest hash drift.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from release_filter_policy import (
        COMPILED_SIDECAR_REQUIRED_FILES,
        FORBIDDEN_FILE_SUFFIXES as FORBIDDEN_PACKAGE_SUFFIXES,
        FORBIDDEN_PATH_COMPONENTS as FORBIDDEN_PACKAGE_NAMES,
        is_admitted_package_executable,
        is_crown_jewel_python,
        is_debug_dll,
        is_forbidden_file_name,
        is_lab_check_executable,
        is_stale_binary_name,
        is_stray_packed_executable,
        is_unapproved_model_path,
        security_policy_violations,
        qt_quick_style_violations,
        qt_quick_style_requirements_missing,
        QT_QUICK_STREAM_DIR,
        QT_QUICK_STYLE_ROOTS,
    )
except ModuleNotFoundError:  # Imported by path from the repository root in tests/tools.
    from tools.release_filter_policy import (
        COMPILED_SIDECAR_REQUIRED_FILES,
        FORBIDDEN_FILE_SUFFIXES as FORBIDDEN_PACKAGE_SUFFIXES,
        FORBIDDEN_PATH_COMPONENTS as FORBIDDEN_PACKAGE_NAMES,
        is_admitted_package_executable,
        is_crown_jewel_python,
        is_debug_dll,
        is_forbidden_file_name,
        is_lab_check_executable,
        is_stale_binary_name,
        is_stray_packed_executable,
        is_unapproved_model_path,
        security_policy_violations,
        qt_quick_style_violations,
        qt_quick_style_requirements_missing,
        QT_QUICK_STREAM_DIR,
        QT_QUICK_STYLE_ROOTS,
    )


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "release" / "orion-package"

ESSENTIAL_PACKAGE_FILES = {
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
    "chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe",
    "libcrypto-3-x64.dll",
    "tls/qschannelbackend.dll",
    "release_manifest.json",
    "release_manifest.sig",
    "security_policy.json",
} | set(COMPILED_SIDECAR_REQUIRED_FILES)

FORBIDDEN_APP_QML_PREFIXES = (
    "OrionNative/",
    "OrionOwner/",
    "OrionStaff/",
    "native_orion/qml/",
)

BINARY_SUFFIXES = {
    ".bmp",
    ".dll",
    ".exe",
    ".ico",
    ".jpg",
    ".jpeg",
    ".mp4",
    ".onnx",
    ".png",
    ".pyd",
    ".wav",
}

TEXT_SCAN_MAX_BYTES = 5 * 1024 * 1024
BINARY_STRING_SCAN_MAX_BYTES = 25 * 1024 * 1024

PACKAGE_BINARY_STRING_SUFFIXES = {
    ".dll",
    ".exe",
    ".pyd",
}

# FIX #10: EVERY first-party binary is stream-scanned regardless of size (opencv/Qt
# third-party DLLs stay excluded — a secret we could leak lives in code we wrote, and
# scanning 60 MB third-party runtimes only adds noise). Includes the standalone sidecar,
# the custom Chiaki stream client, the packet-bridge host, and the server-shard chain
# (the unpacked broker + the Lethe-packed inner payload; the bootstrap ships AS
# OrionNative.exe and is already covered).
ORION_BINARY_STRING_SCAN_NAMES = {
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
    "OrionSidecar.exe",
    "OrionStream.exe",
    "VeniceNetSvc.exe",
    "OrionActivate.exe",
    "OrionNative.packed.exe",
}

SOURCE_FINDING_ALLOWLIST = {
    ("tests/test_security_audit.py", "CONCRETE_DEV_KEY"),
    ("tests/test_security_audit.py", "GUMROAD_TOKEN"),
    # Local deterministic HMAC fixture (18 chars), injected only into mocked webhook tests.
    ("tests/backend/test_webhooks.py", "SELLHUB_SECRET"),
}


@dataclasses.dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    path: str
    detail: str

    def format(self) -> str:
        return f"[{self.severity}] {self.code}: {self.path} - {self.detail}"


SECRET_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "HIGH",
        "CONCRETE_DEV_KEY",
        re.compile(r"\bNVDEV-\d{8}-[A-F0-9]{8,}\b"),
    ),
    (
        "CRITICAL",
        "PRIVATE_KEY_BLOCK",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    (
        "HIGH",
        "STRIPE_SECRET_KEY",
        re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    ),
    (
        "HIGH",
        "GITHUB_TOKEN",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "HIGH",
        "AWS_ACCESS_KEY",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "HIGH",
        "AWS_TEMP_ACCESS_KEY",
        re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    ),
    (
        "CRITICAL",
        "DISCORD_BOT_TOKEN",
        re.compile(r"\b(?:mfa\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{23,32}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{24,64})\b"),
    ),
    (
        "HIGH",
        "CLOUDFLARE_TOKEN",
        re.compile(r"\b[A-Za-z0-9_-]{40,}\b(?=.*cloudflare)", re.IGNORECASE),
    ),
    (
        "HIGH",
        "SELLHUB_SECRET",
        re.compile(
            r"\b(?:sellhub|webhook)[A-Za-z0-9_\- ]{0,32}(?:secret|token|key)"
            r"[A-Za-z0-9_\- ]{0,16}[:=]"
            r"(?!\s*[`\"']?(?:/orion/|arn:aws:ssm:|\{\{resolve:ssm|<))"
            r"\s*[^\n#]{16,}",
            re.IGNORECASE,
        ),
    ),
    (
        "HIGH",
        "GUMROAD_TOKEN",
        re.compile(
            r"\bgumroad[A-Za-z0-9_/\- ]{0,48}(?:token|secret)\b"
            r"[^\n#]{0,80}\b[A-Za-z0-9_-]{24,}\b",
            re.IGNORECASE,
        ),
    ),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(path: Path, root: Path = ROOT) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in sample


def read_text_limited(path: Path) -> str:
    data = path.read_bytes()[:TEXT_SCAN_MAX_BYTES]
    return data.decode("utf-8", errors="ignore")


def should_scan_binary_strings(path: Path) -> bool:
    return path.name in ORION_BINARY_STRING_SCAN_NAMES and path.suffix.lower() in PACKAGE_BINARY_STRING_SUFFIXES


def printable_strings(data: bytes, minimum: int = 8) -> str:
    chunks: list[str] = []
    current = bytearray()
    for value in data:
        if 32 <= value <= 126:
            current.append(value)
        else:
            if len(current) >= minimum:
                chunks.append(current.decode("ascii", errors="ignore"))
            current.clear()
    if len(current) >= minimum:
        chunks.append(current.decode("ascii", errors="ignore"))

    # Windows binaries often store literals as UTF-16LE. A best-effort decoded
    # pass catches obvious embedded keys without treating this as a decompiler.
    utf16 = data.decode("utf-16le", errors="ignore")
    current_chars: list[str] = []
    for ch in utf16:
        code = ord(ch)
        if 32 <= code <= 126:
            current_chars.append(ch)
        else:
            if len(current_chars) >= minimum:
                chunks.append("".join(current_chars))
            current_chars.clear()
    if len(current_chars) >= minimum:
        chunks.append("".join(current_chars))
    return "\n".join(chunks)


def scan_secret_text(path_label: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for severity, code, pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(
                Finding(
                    severity=severity,
                    code=code,
                    path=path_label,
                    detail=f"matched {code}; keep this only in ignored local env files",
                )
            )
    return findings


def git_tracked_files(root: Path) -> list[Path]:
    """Return every file that could be committed accidentally.

    This includes tracked files and non-ignored untracked files.  Ignored local env/key files stay
    out of scope, but a freshly pasted prompt containing a credential must fail the audit before
    its first commit rather than becoming visible only after it enters history.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        # This inventory is the boundary for the source-secret scan.  Returning an
        # empty list here made a missing/broken Git executable indistinguishable
        # from a clean tree and let the release audit fail open.
        raise RuntimeError("git source inventory failed") from exc
    names = [name for name in result.stdout.decode("utf-8", errors="ignore").split("\0") if name]
    return [root / name for name in names]


def audit_tracked_source(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        source_files = git_tracked_files(root)
    except RuntimeError:
        return [
            Finding(
                severity="HIGH",
                code="SOURCE_INVENTORY_FAILED",
                path=rel(root, root),
                detail="git ls-files failed; source secret coverage is incomplete",
            )
        ]
    for path in source_files:
        if not path.is_file() or path.stat().st_size > TEXT_SCAN_MAX_BYTES:
            continue
        if not is_probably_text(path):
            continue
        path_label = rel(path, root)
        for finding in scan_secret_text(path_label, read_text_limited(path)):
            if (path_label, finding.code) not in SOURCE_FINDING_ALLOWLIST:
                findings.append(finding)
    return findings


def audit_executable_admission(package_dir: Path, *, server_shard: bool = False) -> list[Finding]:
    """Profile-aware POSITIVE admission for every executable in the package.

    The copy filter already drops an arbitrary top-level ``*.exe`` during packaging.
    This is the independent twin that rejects the same set in a package that was
    assembled or signed elsewhere, keeping the two gates in lockstep: `random_tool.exe`
    is a finding here exactly as it is dropped there, and `OrionNative.packed.exe` is
    admitted ONLY when the shard profile is explicitly selected.
    """
    findings: list[Finding] = []
    if not package_dir.exists():
        return findings
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".exe":
            continue
        rel_name = path.relative_to(package_dir).as_posix()
        if is_admitted_package_executable(rel_name, server_shard=server_shard):
            continue
        detail = "executable is not on the release allowlist for this package profile"
        if rel_name.lower().endswith(".packed.exe"):
            detail = ("packed payload is admitted only in a server-shard package "
                      "(pass --server-shard to audit a shard release)")
        findings.append(
            Finding(
                severity="HIGH",
                code="UNADMITTED_EXECUTABLE_PACKAGED",
                path=f"{rel(package_dir)}/{rel_name}",
                detail=detail,
            )
        )
    return findings


def audit_package_structure(package_dir: Path, *, server_shard: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    if not package_dir.exists():
        return [
            Finding(
                severity="HIGH",
                code="PACKAGE_MISSING",
                path=rel(package_dir),
                detail="strict release package has not been generated",
            )
        ]

    present = {path.relative_to(package_dir).as_posix() for path in package_dir.rglob("*") if path.is_file()}
    audience = "internal"
    manifest_path = package_dir / "release_manifest.json"
    if manifest_path.is_file():
        try:
            audience = json.loads(manifest_path.read_text(encoding="utf-8")).get("audience", "internal")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass  # The independent manifest audit reports malformed content.
    essentials = ESSENTIAL_PACKAGE_FILES
    if audience == "customer":
        essentials = essentials - {"OrionOwner.exe", "OrionStaff.exe"}
    for essential in sorted(essentials):
        if essential not in present:
            findings.append(
                Finding(
                    severity="HIGH",
                    code="PACKAGE_ESSENTIAL_MISSING",
                    path=f"{rel(package_dir)}/{essential}",
                    detail="required runtime/security file is absent from package",
                )
            )
    # [ORION_QT_STYLE_PRUNE 2026-09-21] The packager prunes to the pinned-style allowlist; this is
    # the INDEPENDENT read of the same policy (release_filter_policy.QT_QUICK_STYLE_ROOTS), so a
    # packager edit that widens the allowlist, or a package assembled by hand, is refused here.
    for violation in qt_quick_style_violations(package_dir):
        findings.append(
            Finding(
                severity="HIGH",
                code="PACKAGE_QT_STYLE_UNPRUNED",
                path=f"{rel(package_dir)}/{violation}",
                detail="Qt Quick style content outside the pinned-style allowlist",
            )
        )
    # [ORION_QT_STYLE_PRUNE 2026-09-21 Codex r2] ...and the other half of the same policy, read
    # independently of the packager: a root whose APP is shipped (OrionNative.exe at the package
    # root, OrionStream.exe in the stream dir) must carry its Qt Quick Controls tree and every
    # file its pinned style needs. A hand-assembled or truncated package renders an empty
    # window, not a crash, so presence of the exe alone is not evidence the UI can load.
    # Fixture packages without those exes are untouched.
    for rel_root, keep in QT_QUICK_STYLE_ROOTS:
        app_root = package_dir / rel_root
        app_exe = app_root / ("OrionStream.exe" if rel_root == QT_QUICK_STREAM_DIR else "OrionNative.exe")
        if not app_exe.is_file():
            continue
        controls = app_root / "qml" / "QtQuick" / "Controls"
        if not controls.is_dir():
            findings.append(
                Finding(
                    severity="HIGH",
                    code="PACKAGE_QT_ROOT_MISSING",
                    path=f"{rel(package_dir)}/{(controls.relative_to(package_dir)).as_posix()}",
                    detail="shipped app has no Qt Quick Controls tree (the UI cannot load)",
                )
            )
            continue
        for missing in qt_quick_style_requirements_missing(app_root, keep):
            findings.append(
                Finding(
                    severity="HIGH",
                    code="PACKAGE_QT_STYLE_INCOMPLETE",
                    path=f"{rel(package_dir)}/{(app_root / missing).relative_to(package_dir).as_posix()}",
                    detail="a file the app's pinned Qt Quick style needs is absent (empty window)",
                )
            )

    for path in package_dir.rglob("*"):
        relative = path.relative_to(package_dir)
        rel_name = relative.as_posix()
        parts = {part.lower() for part in relative.parts}
        if any(part in FORBIDDEN_PACKAGE_NAMES for part in parts):
            findings.append(
                Finding(
                    severity="HIGH",
                    code="FORBIDDEN_PACKAGE_ARTIFACT",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail="local/dev/runtime artifact must not ship",
                )
            )
            continue
        if path.is_file() and path.name.lower().endswith("tests.exe"):
            findings.append(
                Finding(
                    severity="HIGH",
                    code="TEST_BINARY_PACKAGED",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail="test executables must not ship",
                )
            )
            continue
        if path.is_file() and (
            is_lab_check_executable(path.name) or is_stray_packed_executable(path.name)
        ):
            # Release blocker (FIX #2): a lab/behaviour-check binary
            # (GreenWindowMathChecks.exe) or a stray *.packed.exe (any name other than
            # the one permitted server-shard payload OrionNative.packed.exe) means the
            # build dir was contaminated — packaging FAILS instead of leaking it.
            findings.append(
                Finding(
                    severity="HIGH",
                    code="LAB_OR_STRAY_EXECUTABLE_PACKAGED",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail="lab/check or stray packer executable must not ship",
                )
            )
            continue
        if path.is_file() and path.name.lower().startswith("qmldbg_"):
            findings.append(
                Finding(
                    severity="CRITICAL",
                    code="QML_DEBUGGER_PLUGIN_PACKAGED",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail="Qt QML debugging plugins must not ship in production",
                )
            )
            continue
        if path.is_file() and is_debug_dll(path):
            findings.append(
                Finding(
                    severity="HIGH",
                    code="DEBUG_RUNTIME_DLL",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail="debug Qt/OpenCV runtime DLL must not ship",
                )
            )
            continue
        if path.is_file() and is_forbidden_file_name(path.name):
            suffix = path.suffix.lower()
            if suffix in FORBIDDEN_PACKAGE_SUFFIXES:
                detail = f"forbidden runtime suffix {suffix}"
            elif is_stale_binary_name(path.name):
                detail = "forbidden stale/backup runtime filename"
            else:
                detail = "forbidden runtime filename"
            findings.append(
                Finding(
                    severity="HIGH",
                    code="FORBIDDEN_PACKAGE_SUFFIX",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail=detail,
                )
            )
            continue
        if path.is_file() and is_crown_jewel_python(relative):
            findings.append(
                Finding(
                    severity="HIGH",
                    code="CROWN_JEWEL_SOURCE_PACKAGED",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail="compiled detector/timing implementation must not ship as readable Python",
                )
            )
            continue
        if path.is_file() and is_unapproved_model_path(relative):
            findings.append(
                Finding(
                    severity="HIGH",
                    code="UNAPPROVED_MODEL_PACKAGED",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail="models/ content is not approved for the shipped pure-CV runtime",
                )
            )
            continue
        if path.is_file() and any(rel_name.startswith(prefix) for prefix in FORBIDDEN_APP_QML_PREFIXES):
            findings.append(
                Finding(
                    severity="HIGH",
                    code="APP_QML_SOURCE_PACKAGED",
                    path=f"{rel(package_dir)}/{rel_name}",
                    detail="Orion app QML source must be compiled into resources, not shipped loose",
                )
            )

    # Profile-aware positive executable admission, in lockstep with the copy filter.
    # Files already reported above (test/lab/stray binaries) are not re-reported.
    already = {finding.path for finding in findings}
    findings.extend(
        finding
        for finding in audit_executable_admission(package_dir, server_shard=server_shard)
        if finding.path not in already
    )
    return findings


def audit_package_manifest(package_dir: Path) -> list[Finding]:
    findings: list[Finding] = []
    manifest_path = package_dir / "release_manifest.json"
    policy_path = package_dir / "security_policy.json"

    if not policy_path.exists():
        # A package without the runtime policy fails OPEN on the client (SecurityCore
        # has nothing telling it to require the manifest), so its absence is a blocker,
        # not a skipped check.
        findings.append(
            Finding("CRITICAL", "SECURITY_POLICY_MISSING", f"{rel(package_dir)}/security_policy.json",
                    "package must ship the fail-closed runtime security policy")
        )
    else:
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            findings.append(
                Finding("HIGH", "SECURITY_POLICY_INVALID_JSON", rel(policy_path), str(exc))
            )
        else:
            # FULL semantic validation against the shared production contract: a
            # correctly SIGNED policy that does not fail closed is still refused.
            for problem in security_policy_violations(policy):
                if problem.startswith("require_release_manifest"):
                    code, severity = "RELEASE_MANIFEST_NOT_REQUIRED", "CRITICAL"
                elif problem.startswith("allow_local_dev_bypass"):
                    code, severity = "LOCAL_DEV_BYPASS_ALLOWED", "CRITICAL"
                elif problem.startswith("unexpected policy schema") or problem.endswith("JSON object"):
                    code, severity = "SECURITY_POLICY_SCHEMA", "HIGH"
                else:
                    code, severity = "SECURITY_POLICY_UNSAFE_VALUE", "CRITICAL"
                findings.append(Finding(severity, code, rel(policy_path), problem))

    if not manifest_path.exists():
        return findings

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [Finding("HIGH", "RELEASE_MANIFEST_INVALID_JSON", rel(manifest_path), str(exc))]

    if manifest.get("schema") != "orion.release_manifest.v1":
        findings.append(
            Finding("HIGH", "RELEASE_MANIFEST_SCHEMA", rel(manifest_path), "unexpected manifest schema")
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        findings.append(
            Finding("HIGH", "RELEASE_MANIFEST_EMPTY", rel(manifest_path), "manifest contains no files")
        )
        return findings

    listed_files: set[str] = set()
    for name, entry in files.items():
        if not isinstance(name, str) or name.startswith("../") or Path(name).is_absolute():
            findings.append(
                Finding("CRITICAL", "UNSAFE_MANIFEST_PATH", rel(manifest_path), f"unsafe entry {name!r}")
            )
            continue
        listed_files.add(name)
        expected = entry.get("sha256") if isinstance(entry, dict) else entry
        candidate = package_dir / name
        if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
            findings.append(
                Finding("HIGH", "MANIFEST_HASH_INVALID", f"{rel(package_dir)}/{name}", "invalid sha256 value")
            )
            continue
        if not candidate.exists():
            findings.append(
                Finding("HIGH", "MANIFEST_FILE_MISSING", f"{rel(package_dir)}/{name}", "listed file is absent")
            )
            continue
        actual = sha256_file(candidate)
        if actual.lower() != expected.lower():
            findings.append(
                Finding("CRITICAL", "MANIFEST_HASH_MISMATCH", f"{rel(package_dir)}/{name}", "package file does not match manifest")
            )

    actual_files = {
        path.relative_to(package_dir).as_posix()
        for path in package_dir.rglob("*")
        if path.is_file() and path.name not in {"release_manifest.json", "release_manifest.sig"}
    }
    for name in sorted(actual_files - listed_files):
        findings.append(
            Finding("HIGH", "UNMANIFESTED_PACKAGE_FILE", f"{rel(package_dir)}/{name}", "runtime file is not covered by release_manifest.json")
        )
    return findings


def _scan_binary_strings_streaming(path: Path) -> list[Finding]:
    """Stream-scan a first-party binary for embedded secrets with NO size cap.

    The whole file is read in overlapping windows so a secret at ANY offset — including
    one past the retired 25 MB cap — is still caught. The overlap (well beyond the
    longest secret pattern) bridges chunk boundaries, and the window length is even so
    UTF-16LE alignment is preserved. Findings are de-duplicated per (code) so the
    overlap does not report the same embedded key twice.
    """
    label = f"{rel(path)}:strings"
    chunk_size = 1 << 20   # 1 MiB working set — bounded memory regardless of file size
    overlap = 8192         # >> the longest secret pattern, so a straddling match is whole
    findings: list[Finding] = []
    seen_codes: set[str] = set()
    carry = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                window = carry + chunk
                for finding in scan_secret_text(label, printable_strings(window)):
                    if finding.code not in seen_codes:
                        seen_codes.add(finding.code)
                        findings.append(finding)
                carry = window[-overlap:]
    except OSError:
        return findings
    return findings


def audit_package_secret_content(package_dir: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not package_dir.exists():
        return findings
    for path in package_dir.rglob("*"):
        if not path.is_file():
            continue
        if should_scan_binary_strings(path):
            # First-party binary: stream the whole file (no cap, FIX #10).
            findings.extend(_scan_binary_strings_streaming(path))
        elif is_probably_text(path) and path.stat().st_size <= TEXT_SCAN_MAX_BYTES:
            findings.extend(scan_secret_text(rel(path), read_text_limited(path)))
    return findings


def audit_package(package_dir: Path, *, server_shard: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(audit_package_structure(package_dir, server_shard=server_shard))
    if package_dir.exists():
        findings.extend(audit_package_manifest(package_dir))
        findings.extend(audit_package_secret_content(package_dir))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--package-dir", type=Path, default=PACKAGE_DIR)
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--package-only", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--server-shard", action="store_true",
                        help="Audit a server-shard package: admits OrionActivate.exe and the "
                             "packed inner payload OrionNative.packed.exe. Without this flag a "
                             "*.packed.exe at the package root is stray packer debris.")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    package_dir = args.package_dir.resolve()
    findings: list[Finding] = []
    if not args.package_only:
        findings.extend(audit_tracked_source(root))
    if not args.source_only:
        findings.extend(audit_package(package_dir, server_shard=args.server_shard))

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 99), f.code, f.path))

    if args.json_output:
        print(json.dumps([dataclasses.asdict(f) for f in findings], indent=2))
    elif findings:
        print("[orion-security] findings:")
        for finding in findings:
            print(f"  - {finding.format()}")
    else:
        print("[orion-security] OK")

    return 2 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
