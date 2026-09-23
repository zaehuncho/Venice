#!/usr/bin/env python3
"""Apply the Lethe packer to an Orion release package, then assemble + verify it.

This script is a wrapper around the standalone Lethe CLI, not a packer
implementation. It keeps the launch-critical pipeline correct and fail-closed:

Modes
-----
* ``--verify-only`` (READ-ONLY, FIX #3): validate an EXISTING packed package's
  release manifest + Ed25519 signature against the PINNED production key, run the
  forbidden/secret scans, enforce full CUSTOMER ACCEPTANCE (zero unmanifested files,
  audience=customer, no Owner/Staff on disk or in the manifest, a security policy
  that fails closed, profile-aware executable admission), and prove the customer
  launch + tamper-refusal smokes. It NEVER copies, packs, regenerates, or signs
  anything. Re-signing lives only in the assembly path below.
* assembly (default): copy the input package into a private, run-specific staging
  root (FIX #5), pack first-party executables (Lethe with restricted DLL search
  dirs, FIX #9), run the INTERNAL Owner/Staff smokes, then STRIP Owner/Staff,
  regenerate + Ed25519-sign the final CUSTOMER manifest, audit, verify, run the
  CUSTOMER startup + tamper smokes, build the zip + signed update manifest, and
  ONLY THEN atomically promote the complete artifact set. Any failure leaves the
  previously published artifacts untouched.
* ``--server-shard`` (FIX #1): assemble the three-exe activation chain
  (broker OrionActivate.exe -> bootstrap staged as OrionNative.exe -> packed
  OrionNative.packed.exe), assert every shard essential is present AND covered by
  the signed customer manifest, and keep the gate FAIL-CLOSED until it is.

Packing is only a tamper-resistance layer. Authorization is still enforced by the
backend.

OPEN RELEASE BLOCKER — the broker's trust anchor (red-team round 2, 2026-09-20):
the signed release manifest is a sound package-INTEGRITY record, but it is NOT a
sufficient sole BOOTSTRAP trust anchor for an unsigned process that verifies itself.
Coverage proves what the signer intended; it cannot force a REPLACEMENT
OrionActivate.exe to run the self-check at all, and Windows resolves the broker's
app-local imports (SecurityCore.dll -> Qt6Network.dll, Qt6Core.dll) BEFORE broker
code executes, so a tampered app-local DLL runs first. What this packer proves is
therefore "the package we published is intact", not "a tampered install refuses to
run". Closing it needs a verifier OUTSIDE the verified object: an OS-enforced
signed launcher / code-integrity (WDAC) policy or a minimal trusted bootstrap that
verifies the broker AND its imports before loading them. The ACL-protected
{autopf}\\Venice install (installer/orion.iss, PrivilegesRequired=admin) raises the
bar — a standard user cannot overwrite the chain — but does not close it.
See docs/ACTIVATION_BROKER_2026-09-19.md.

The smokes below are an OFFLINE INTEGRITY PROXY (they replicate the native startup
gate's decision), NOT an executed launch of the packed binaries. The clean-VM
broker -> bootstrap -> packed-app launch + per-class tamper proof remains a
MANDATORY blocking rig test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "release" / "orion-package"
DEFAULT_OUTPUT = ROOT / "release" / "orion-package-packed"
RELEASE_DIR = ROOT / "release"
PACK_ARCHIVE_DIR = ROOT / "archive" / "local-packed"

DEFAULT_TARGETS = (
    "OrionOwner.exe",
    "OrionStaff.exe",
    "OrionNative.exe",
    "OrionUpdater.exe",
)

# Default external packer: the standalone Lethe CLI (extracted out of this repo).
# Override the location with the LETHE_CLI env var, or pass --packer-command to
# use a different packer entirely (e.g. VMProtect).
DEFAULT_LETHE_CLI = os.environ.get(
    "LETHE_CLI", r"C:\Users\aaron\Desktop\Lethe\lethe.py"
)
DEFAULT_LETHE_ROOT = os.environ.get(
    "LETHE_ROOT", str(Path(DEFAULT_LETHE_CLI).resolve().parent)
)
# Round-3 build outputs (rig-only; overridable). The bootstrap is staged AS
# OrionNative.exe and the broker ships UNPACKED as OrionActivate.exe.
DEFAULT_BOOTSTRAP_EXE = os.environ.get(
    "LETHE_SHARD_BOOTSTRAP",
    str(Path(DEFAULT_LETHE_ROOT) / "bootstrap" / "build" / "Release" / "LetheShardBootstrap.exe"),
)
DEFAULT_BROKER_EXE = os.environ.get(
    "ORION_ACTIVATE_BROKER",
    str(ROOT / "native_orion" / "build" / "Release" / "OrionActivate.exe"),
)
DEFAULT_SHARD_URL = os.environ.get("LETHE_SHARD_URL", "https://api.zaeorion.com")

# The internal admin tools (present only in a full/internal package). They are
# smoke-tested in the INTERNAL stage, then STRIPPED before the customer manifest.
INTERNAL_ADMIN_BINARIES = ("OrionOwner.exe", "OrionStaff.exe")
# The customer launch path proven by the packaging-time startup smoke.
CUSTOMER_STARTUP_BINARIES = ("OrionNative.exe", "OrionUpdater.exe")

# Server-shard chain fixed names.
SHARD_PACKED_PAYLOAD_NAME = "OrionNative.packed.exe"
SHARD_BOOTSTRAP_STAGED_AS = "OrionNative.exe"
SHARD_BROKER_NAME = "OrionActivate.exe"

# Shard essentials that MUST be present AND explicitly covered by the signed
# customer manifest. Fail closed if any is missing (FIX #1). Qt plugins and any
# VC runtime deps that are actually present are discovered and additionally
# required to be manifest-covered (never unmanifested).
SERVER_SHARD_REQUIRED_FILES = (
    SHARD_BROKER_NAME,          # unpacked activation broker (Ed25519-manifest anchored)
    SHARD_BOOTSTRAP_STAGED_AS,  # the Lethe bootstrap, staged AS OrionNative.exe
    SHARD_PACKED_PAYLOAD_NAME,  # the packed inner payload
    "SecurityCore.dll",
    "Qt6Core.dll",
    "Qt6Network.dll",
    "libcrypto-3-x64.dll",
)

# Credential flags must NEVER appear on the packer command line (FIX #10): Lethe
# reads shard secrets from the environment (LETHE_SHARD_ADMIN_SECRET,
# LETHE_SHARD_EDGE_AUTH, LETHE_SHARD_ADMIN_TOTP, LETHE_SHARD_PIN_PEM). Inline
# credentials would leak into argv, process listings, and this repo's logs.
CREDENTIAL_BEARING_ARG_TOKENS = (
    "--shard-auth",
    "--shard-edge-auth",
    "--shard-admin-totp",
    "--auth",
    "--secret",
    "--password",
    "--token",
    "--apikey",
    "--api-key",
)

SERVER_SHARD_RELEASE_BLOCKER = (
    "server-shard release is FAIL-CLOSED: the assembled three-exe chain "
    "(signed activation broker OrionActivate.exe -> bootstrap staged as "
    "OrionNative.exe -> packed OrionNative.packed.exe) plus every shard essential "
    "must be present AND covered by the Ed25519-signed customer manifest before a "
    "customer package may be emitted. See docs/SERVER_SHARD_2026-09-19.md."
)


# ── lazy module loaders (tools/ is a namespace package under ROOT) ──────────────

_PKG = None
_VRI = None
_AUDIT = None


def _pkg():
    global _PKG
    if _PKG is None:
        sys.path.insert(0, str(ROOT))
        from tools import package_orion_release as module
        _PKG = module
    return _PKG


def _vri():
    global _VRI
    if _VRI is None:
        sys.path.insert(0, str(ROOT))
        from tools import verify_release_integrity as module
        _VRI = module
    return _VRI


def _audit():
    global _AUDIT
    if _AUDIT is None:
        sys.path.insert(0, str(ROOT))
        from tools import security_audit as module
        _AUDIT = module
    return _AUDIT


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── packer command handling ─────────────────────────────────────────────────────

def quote_cmd_arg(path: Path) -> str:
    text = str(path)
    if '"' in text:
        raise ValueError(f"path contains a double quote and cannot be safely passed to shell: {text}")
    return f'"{text}"'


def format_packer_command(template: str, input_path: Path, output_path: Path) -> str:
    if "{input}" not in template or "{output}" not in template:
        raise ValueError("packer command must contain both {input} and {output} placeholders")
    return template.replace("{input}", quote_cmd_arg(input_path)).replace("{output}", quote_cmd_arg(output_path))


def assert_no_credential_bearing_args(template: str) -> None:
    """FIX #10: refuse a packer template that carries credentials on the command line."""
    lowered = template.lower()
    hits = sorted({token for token in CREDENTIAL_BEARING_ARG_TOKENS if token in lowered})
    if hits:
        raise ValueError(
            "packer command template must not carry credentials on the command line; "
            "inject them via the environment/secret manager (LETHE_SHARD_ADMIN_SECRET, "
            "LETHE_SHARD_EDGE_AUTH, LETHE_SHARD_ADMIN_TOTP, LETHE_SHARD_PIN_PEM). "
            f"Offending token(s): {', '.join(hits)}"
        )


def redact_packer_command(command: str) -> str:
    """A log-safe rendering of a packer command: the tool + flags only, never a value.

    Everything after a ``--flag`` that is not itself a flag is replaced with ``<redacted>``
    so no path or accidental inline value is ever printed/recorded.
    """
    safe: list[str] = []
    tokens = command.split()
    expect_value_flag = None
    for token in tokens:
        if token.startswith("--") or token.startswith("-"):
            safe.append(token)
            expect_value_flag = token
        elif token in ("{input}", "{output}"):
            safe.append(token)
            expect_value_flag = None
        else:
            safe.append("<redacted>")
            expect_value_flag = None
    return " ".join(safe)


def run(command: "list[str] | str", cwd: Path | None = None) -> None:
    """Run a NON-packer command, echoing it (safe: no secrets in these)."""
    print(f"[orion-pack] run: {command if isinstance(command, str) else ' '.join(command)}")
    subprocess.run(command, cwd=cwd or ROOT, shell=isinstance(command, str), check=True)


def run_packer_command(command: str, cwd: Path | None = None) -> None:
    """Run the packer, logging a REDACTED command line only (FIX #10)."""
    print(f"[orion-pack] pack: {redact_packer_command(command)}")
    subprocess.run(command, cwd=cwd or ROOT, shell=True, check=True)


def parse_targets(raw: str) -> list[str]:
    targets = [item.strip().replace("\\", "/") for item in raw.split(",") if item.strip()]
    if not targets:
        raise ValueError("at least one pack target is required")
    return targets


def validate_release_targets(targets: list[str]) -> None:
    """Enforce the production packer's loader-safe EXE-only boundary."""
    non_exe = [target for target in targets if Path(target).suffix.lower() != ".exe"]
    if non_exe:
        rendered = ", ".join(non_exe)
        raise ValueError(
            "production release packing is EXE-only; refused non-EXE target(s): "
            f"{rendered}. DLL release packing remains blocked until "
            "loader-safe deferred initialization exists"
        )


def copy_package(source: Path, dest: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"input package does not exist: {source}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def run_packer(package_dir: Path, targets: list[str], command_template: str) -> list[dict]:
    """Pack each first-party EXE target IN PLACE (non-shard assembly)."""
    validate_release_targets(targets)
    packed: list[dict] = []
    temp_dir = Path(tempfile.mkdtemp(prefix="orion-pack-targets-"))
    try:
        for rel in targets:
            target = package_dir / rel
            if not target.exists():
                raise FileNotFoundError(f"packer target is missing from package: {rel}")
            packed_output = temp_dir / target.name
            command = format_packer_command(command_template, target, packed_output)
            run_packer_command(command)
            if not packed_output.exists() or packed_output.stat().st_size <= 0:
                raise RuntimeError(f"packer did not produce a non-empty output for {rel}: {packed_output}")
            original_size = target.stat().st_size
            shutil.move(str(packed_output), target)
            packed.append({
                "target": rel,
                "original_size": original_size,
                "packed_size": target.stat().st_size,
            })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return packed


# ── packer provenance (FIX #7) ───────────────────────────────────────────────────

_PROVENANCE_FILES = (
    "lethe.py",
    "packer/orchestrator.py",
    "packer/assemble.py",
    "bootstrap/shard_bootstrap.c",
)


def _git_output(root: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            f"[orion-pack] REFUSED: packer provenance unavailable "
            f"(git {' '.join(args)} failed in {root}); a release must record a clean, "
            f"identifiable packer tree"
        ) from exc
    return completed.stdout.strip()


def collect_packer_provenance(lethe_root: Path, *, expected_commit: str = "",
                              allow_dirty: bool = False) -> dict:
    """Record the packer's commit + per-file sha256, and REFUSE a dirty/divergent tree.

    Pinning a specific clean commit (``--expected-packer-commit``) is the owner's step;
    by default this refuses only an UNCOMMITTED (dirty) packer tree so a release can
    never be produced from unversioned packer sources.
    """
    lethe_root = Path(lethe_root).resolve()
    if not lethe_root.is_dir():
        raise SystemExit(f"[orion-pack] REFUSED: Lethe packer tree not found: {lethe_root}")
    commit = _git_output(lethe_root, ["rev-parse", "HEAD"])
    porcelain = _git_output(lethe_root, ["status", "--porcelain"])
    dirty = bool(porcelain.strip())
    if dirty and not allow_dirty:
        raise SystemExit(
            "[orion-pack] REFUSED: Lethe packer tree is DIRTY (uncommitted changes); a "
            "release must pin a clean packer commit. Commit/stash and pin the commit, or "
            "pass --allow-dirty-packer for a NON-release test build.\n"
            + porcelain
        )
    if expected_commit and commit.lower() != expected_commit.strip().lower():
        raise SystemExit(
            f"[orion-pack] REFUSED: Lethe packer HEAD {commit} does not match the pinned "
            f"--expected-packer-commit {expected_commit}"
        )
    files: dict[str, str] = {}
    for rel in _PROVENANCE_FILES:
        path = lethe_root / rel
        if path.is_file():
            files[rel] = sha256_file(path)
    stub_dir = lethe_root / "stub" / "prebuilt"
    if stub_dir.is_dir():
        for path in sorted(p for p in stub_dir.rglob("*") if p.is_file()):
            files[f"stub/prebuilt/{path.relative_to(stub_dir).as_posix()}"] = sha256_file(path)
    return {
        "root": str(lethe_root),
        "commit": commit,
        "dirty": dirty,
        "files": files,
    }


# ── run-specific atomic staging (FIX #5) ─────────────────────────────────────────

def create_pack_staging_root(output_dir: Path) -> Path:
    """Create a private, same-volume staging root beside the published output dir."""
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(
        prefix=f".{output_dir.name}.staging-",
        dir=output_dir.parent,
    )).resolve()


def _validate_pack_staging_root(staging_root: Path, output_dir: Path) -> tuple[Path, Path]:
    staging_root = staging_root.resolve()
    output_dir = output_dir.resolve()
    expected_prefix = f".{output_dir.name}.staging-"
    if staging_root.parent != output_dir.parent or not staging_root.name.startswith(expected_prefix):
        raise ValueError(
            f"Refusing non-packager staging root {staging_root}; expected a sibling named "
            f"{expected_prefix}*"
        )
    return staging_root, output_dir


def cleanup_pack_staging_root(staging_root: Path, output_dir: Path) -> None:
    staging_root, _ = _validate_pack_staging_root(staging_root, output_dir)
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)


PUBLISH_JOURNAL_NAME = ".orion-pack-publish.journal.json"
PUBLISH_JOURNAL_SCHEMA = "orion.pack_publish_journal.v1"


def publish_journal_path(release_dir: Path | None = None) -> Path:
    return Path(release_dir or RELEASE_DIR) / PUBLISH_JOURNAL_NAME


def _write_journal_atomically(path: Path, data: dict) -> None:
    """Durably record the publish plan/state: tmp file -> fsync -> atomic replace.

    The journal is what makes a KILLED promotion recoverable. Writing it in place would
    reintroduce the very torn-write problem it exists to solve.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def _journal_entries(staged_items: list[tuple[Path, Path]], run_archive: Path) -> list[dict]:
    return [
        {
            "staged": str(Path(staged).resolve()),
            "published": str(Path(published).resolve()),
            "archived": str((run_archive / Path(published).name).resolve()),
            "state": "planned",
        }
        for staged, published in staged_items
    ]


def _discard_path(path: Path) -> None:
    """Remove a published artifact (file or the package directory) during a roll-back."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def recover_publish_journal(release_dir: Path | None = None) -> str | None:
    """Repair a promotion that was KILLED mid-move (FIX #7, round 2).

    Handled exceptions already roll back in-process, but a process kill / power loss
    BETWEEN two individual moves can leave a MIXED published set (new package dir beside
    a stale zip + update manifest). Every move is journalled before and after it runs, so
    the next run can finish the transaction deterministically:

    * every pending artifact still staged (or already published) -> ROLL FORWARD to the
      complete new set;
    * otherwise -> ROLL BACK to the complete old set from the run archive.

    Readers therefore only ever observe a complete set. Returns the action taken
    ("roll-forward" / "roll-back", or None when there is nothing to recover).
    """
    journal = publish_journal_path(release_dir)
    if not journal.is_file():
        return None
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"[orion-pack] REFUSED: publish journal {journal} is unreadable ({exc}); the "
            "published artifact set may be MIXED. Restore from archive/local-packed and "
            "remove the journal by hand.")
    entries = data.get("entries") or []
    pending = [e for e in entries if e.get("state") != "published"]
    can_roll_forward = all(
        Path(e["staged"]).exists() or Path(e["published"]).exists() for e in pending)

    if can_roll_forward:
        for entry in entries:
            staged, published = Path(entry["staged"]), Path(entry["published"])
            if entry.get("state") == "published":
                continue
            if published.exists() and not staged.exists():
                entry["state"] = "published"
                _write_journal_atomically(journal, data)
                continue
            archived = Path(entry["archived"])
            if published.exists() and not archived.exists():
                archived.parent.mkdir(parents=True, exist_ok=True)
                published.replace(archived)
            published.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(published)
            entry["state"] = "published"
            _write_journal_atomically(journal, data)
        action = "roll-forward"
    else:
        for entry in reversed(entries):
            staged, published = Path(entry["staged"]), Path(entry["published"])
            archived = Path(entry["archived"])
            if entry.get("state") == "published" and published.exists() and not staged.exists():
                if staged.parent.is_dir():
                    published.replace(staged)   # park the new artifact back in staging
                else:
                    # Staging is already gone (its cleanup won the race). The new
                    # artifact is unreachable anyway, so DISCARD it: the complete OLD
                    # set is restored from the archive below.
                    _discard_path(published)
            if archived.exists() and not published.exists():
                published.parent.mkdir(parents=True, exist_ok=True)
                archived.replace(published)
            entry["state"] = "rolled-back"
            _write_journal_atomically(journal, data)
        action = "roll-back"

    journal.unlink(missing_ok=True)
    print(f"[orion-pack] recovered an interrupted publish ({action}): the published "
          "artifact set is complete again")
    return action


def _rollback_promotion(promoted: list[tuple[Path, Path]],
                        archived: list[tuple[Path, Path]], journal: Path) -> None:
    """Return promoted artifacts to staging, restore the archived set, drop the journal.

    This is the HANDLED-failure path. A process kill runs none of it, which is exactly
    why the journal exists: recover_publish_journal() replays it on the next run.
    """
    for staged, published in reversed(promoted):
        if published.exists() and not staged.exists():
            published.replace(staged)
    for published, archived_copy in reversed(archived):
        if archived_copy.exists() and not published.exists():
            archived_copy.replace(published)
    journal.unlink(missing_ok=True)


def promote_pack_output(staged_items: list[tuple[Path, Path]], archive_dir: Path,
                        release_dir: Path | None = None) -> Path | None:
    """Promote a fully-built + verified artifact set under a DURABLE journal.

    ``staged_items`` maps each staged path to its published path. The plan is journalled
    (fsync'd) BEFORE the first move and updated after every individual move, so:

    * a handled failure rolls back in-process (prior artifacts preserved intact), and
    * a KILL between two moves leaves a journal that ``recover_publish_journal()``
      replays on the next run - forward to the complete new set while the staged
      artifacts survive, back to the complete old set otherwise.

    A reader therefore never observes a half-published mixture of old and new.
    """
    archive_dir = Path(archive_dir)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    run_archive = archive_dir / f"packed-{stamp}"
    journal = publish_journal_path(release_dir)
    data = {
        "schema": PUBLISH_JOURNAL_SCHEMA,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "archive": str(run_archive.resolve()),
        "entries": _journal_entries(staged_items, run_archive),
    }
    _write_journal_atomically(journal, data)

    archived: list[tuple[Path, Path]] = []   # (published, archived_copy)
    promoted: list[tuple[Path, Path]] = []    # (staged, published)
    try:
        for index, (_staged, published) in enumerate(staged_items):
            if published.exists():
                run_archive.mkdir(parents=True, exist_ok=True)
                archived_copy = run_archive / published.name
                published.replace(archived_copy)
                archived.append((published, archived_copy))
                data["entries"][index]["state"] = "archived"
                _write_journal_atomically(journal, data)
        for index, (staged, published) in enumerate(staged_items):
            published.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(published)
            promoted.append((staged, published))
            data["entries"][index]["state"] = "published"
            _write_journal_atomically(journal, data)
    except BaseException:
        _rollback_promotion(promoted, archived, journal)
        raise
    journal.unlink(missing_ok=True)
    return run_archive if archived else None


# ── manifest helpers ─────────────────────────────────────────────────────────────

def _manifest_version(package_dir: Path) -> str | None:
    manifest = package_dir / "release_manifest.json"
    if manifest.is_file():
        try:
            return json.loads(manifest.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, TypeError):
            return None
    return None


def write_internal_manifest(package_dir: Path, signing_key: Path, version: str | None) -> dict:
    """Sign a manifest that covers EVERY shipped binary, incl. Owner/Staff (audience internal)."""
    return _pkg().write_release_manifest(
        package_dir, version=version, signing_key=signing_key, customer=False)


def write_customer_manifest(package_dir: Path, signing_key: Path, version: str | None) -> dict:
    """Sign the final CUSTOMER manifest (Owner/Staff excluded; audience customer)."""
    return _pkg().write_release_manifest(
        package_dir, version=version, signing_key=signing_key, customer=True)


def regenerate_manifest(package_dir: Path, signing_key: Path) -> dict:
    """Back-compat shim: regenerate + sign the CUSTOMER manifest for *package_dir*."""
    return write_customer_manifest(package_dir, signing_key, _manifest_version(package_dir))


def audit_package(package_dir: Path, *, server_shard: bool = False) -> None:
    command = [sys.executable, "tools/security_audit.py", "--package-only",
               "--package-dir", str(package_dir)]
    if server_shard:
        # Admits exactly the two shard chain members (broker + packed inner payload);
        # every other executable stays unadmitted.
        command.append("--server-shard")
    run(command)


# ── integrity probe + smokes (FIX #6) ────────────────────────────────────────────

def _pinned_probe(public_key_b64: str = "", key_id: str | None = None):
    """Return a position-independent OFFLINE INTEGRITY PROXY for the NATIVE startup gate.

    The GUI OrionNative.exe has no non-interactive self-check flag; its production
    startup runs SecurityManager::verifyReleaseIntegrity() (manifest + Ed25519 signature
    + per-file sha256 + forbidden/secret scan) and fails closed on any mismatch. That is
    exactly verify_release_integrity.verify(), so a green probe means the packed customer
    binaries would not be LOCKED OUT by the integrity gate from this directory.

    LIMITS (red-team round 2): this executes NOTHING. It cannot detect a PE-load
    failure, a missing Qt plugin, Lethe/packer incompatibility, a bootstrap/shard
    failure, or real DLL-search behaviour. It is a preflight, and it is adequate ONLY
    while the clean-VM launch (broker -> bootstrap -> packed app -> updater, from a
    renamed dir, unrelated CWD, scrubbed env, then every tamper class re-run against
    the real processes) remains a MANDATORY blocking rig test. On the rig, pass a
    runner to exec the real binaries instead of probing.
    """
    key_id = key_id or _pkg().ED25519_KEY_ID

    def probe(package_dir: Path, cwd: Path | None = None, env: dict | None = None) -> dict:
        return _vri().verify(package_dir, public_key_b64, key_id)

    return probe


def _default_startup_runner(exe: Path, args: list[str], cwd: Path, env: dict) -> int:
    proc = subprocess.run([str(exe), *args], cwd=str(cwd), env=env, check=False)
    return proc.returncode


def _scrubbed_dev_env() -> dict:
    """A clean environment with dev escape hatches removed (FIX #6)."""
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if (upper.startswith("ORION_") or upper.startswith("LETHE_")
                or upper in {"ORION_UPDATE_SIGNING_KEY_PEM"}):
            env.pop(key, None)
    return env


def _assert_present_and_covered(package_dir: Path, names: list[str]) -> None:
    manifest_path = package_dir / "release_manifest.json"
    try:
        files = json.loads(manifest_path.read_text(encoding="utf-8")).get("files") or {}
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"release manifest unreadable for coverage check: {exc}") from exc
    missing = [n for n in names if not (package_dir / n).is_file()]
    uncovered = [n for n in names if n not in files]
    if missing or uncovered:
        raise RuntimeError(
            "customer launch binaries not present/covered — "
            f"missing on disk: {missing or 'none'}; not manifest-covered: {uncovered or 'none'}"
        )


def internal_admin_smoke(package_dir: Path, *, runner=None, probe=None) -> None:
    """Prove the Owner/Staff tools pass startup integrity in the INTERNAL stage (FIX #4)."""
    present = [name for name in INTERNAL_ADMIN_BINARIES if (package_dir / name).is_file()]
    if not present:
        print("[orion-pack] internal admin smoke: no Owner/Staff binaries present; skipping")
        return
    if runner is not None:
        env = _scrubbed_dev_env()
        for name in present:
            rc = runner(package_dir / name, ["--check-startup-security"], package_dir, env)
            if rc != 0:
                raise RuntimeError(f"internal admin startup check failed for {name} (exit {rc})")
        print(f"[orion-pack] internal admin startup OK ({', '.join(present)})")
        return
    probe = probe or _pinned_probe()
    result = probe(package_dir, package_dir, _scrubbed_dev_env())
    if not result.get("ok"):
        raise RuntimeError(
            "internal admin startup integrity would FAIL: " + "; ".join(result.get("errors", [])))
    print(f"[orion-pack] internal admin startup integrity OK — offline proxy, not an "
          f"executed launch ({', '.join(present)})")


def customer_startup_smoke(package_dir: Path, *, extra_binaries: tuple[str, ...] = (),
                           probe=None) -> None:
    """Prove the CUSTOMER launch from a RENAMED clean dir, unrelated CWD, scrubbed env (FIX #6)."""
    probe = probe or _pinned_probe()
    binaries = list(CUSTOMER_STARTUP_BINARIES) + list(extra_binaries)
    temp_root = Path(tempfile.mkdtemp(prefix="orion-pack-launch-"))
    try:
        # A RENAMED clean directory (not orion-package-packed) proves the startup gate is
        # position-independent — it must not depend on the folder name.
        renamed = temp_root / "Venice"
        shutil.copytree(package_dir, renamed)
        _assert_present_and_covered(renamed, binaries)
        unrelated_cwd = temp_root / "unrelated-cwd"
        unrelated_cwd.mkdir()
        result = probe(renamed, unrelated_cwd, _scrubbed_dev_env())
        if not result.get("ok"):
            raise RuntimeError(
                "customer startup integrity would FAIL (packed app would lock on launch): "
                + "; ".join(result.get("errors", [])))
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    print(f"[orion-pack] customer startup integrity OK — OFFLINE PROXY, not an executed "
          f"launch; the clean-VM launch remains a blocking rig test ({', '.join(binaries)})")


def _first_manifest_covered_dll(package_dir: Path) -> Path:
    for candidate in ("SecurityCore.dll", "OrionCommon.dll", "libcrypto-3-x64.dll"):
        if (package_dir / candidate).is_file():
            return package_dir / candidate
    for path in sorted(package_dir.rglob("*.dll")):
        return path
    raise RuntimeError("no DLL present to exercise the DLL-tamper class")


def _flip_first_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    if not data:
        data = bytearray(b"\x00")
    data[0] ^= 0xFF
    path.write_bytes(bytes(data))


def tamper_variants(package_dir: Path, *, server_shard: bool) -> list[tuple[str, "callable"]]:
    variants: list[tuple[str, callable]] = [
        ("delete-manifest", lambda p: (p / "release_manifest.json").unlink()),
        ("tamper-dll", lambda p: _flip_first_byte(_first_manifest_covered_dll(p))),
        ("tamper-signature", lambda p: (p / "release_manifest.sig").write_bytes(b"not-a-valid-signature\n")),
        ("tamper-policy", lambda p: (p / "security_policy.json").write_text(
            '{"schema":"orion.security_policy.v1","require_release_manifest":false}', encoding="utf-8")),
    ]
    if server_shard:
        variants.append(
            ("tamper-packed-payload", lambda p: _flip_first_byte(p / SHARD_PACKED_PAYLOAD_NAME)))
    return variants


def tamper_refusal_smoke(package_dir: Path, *, server_shard: bool = False, probe=None) -> None:
    """Each tamper class (manifest/DLL/signature/policy/[shard]payload) must FAIL closed (FIX #6)."""
    probe = probe or _pinned_probe()
    for label, mutate in tamper_variants(package_dir, server_shard=server_shard):
        temp_root = Path(tempfile.mkdtemp(prefix="orion-pack-tamper-"))
        try:
            tampered = temp_root / "orion-package"
            shutil.copytree(package_dir, tampered)
            mutate(tampered)
            result = probe(tampered, tampered, _scrubbed_dev_env())
            if result.get("ok"):
                raise RuntimeError(
                    f"tamper class {label!r} was ACCEPTED by the integrity gate "
                    "(it must fail closed)")
            print(f"[orion-pack] tamper refusal OK: {label}")
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


# ── server-shard assembly + gate (FIX #1) ────────────────────────────────────────

def _verify_stageable(source: Path, label: str) -> None:
    source = Path(source)
    if not source.is_file() or source.stat().st_size <= 0:
        raise SystemExit(
            f"[orion-pack] REFUSED: server-shard {label} is missing or empty: {source}. "
            "Build it on a pinned clean tree before packing a shard release."
        )


def stage_server_shard_chain(package_dir: Path, *, command_template: str,
                             bootstrap_exe: Path, broker_exe: Path) -> dict:
    """Assemble the three-exe chain into *package_dir* (an internal staging copy).

    1. Lethe-pack the app OrionNative.exe -> OrionNative.packed.exe (shard flags).
    2. Stage the verified LetheShardBootstrap.exe AS OrionNative.exe.
    3. Stage the UNPACKED broker AS OrionActivate.exe.
    """
    app = package_dir / SHARD_BOOTSTRAP_STAGED_AS
    if not app.is_file():
        raise SystemExit(
            f"[orion-pack] REFUSED: server-shard input package has no {SHARD_BOOTSTRAP_STAGED_AS} "
            "to pack")
    packed = package_dir / SHARD_PACKED_PAYLOAD_NAME
    command = format_packer_command(command_template, app, packed)
    run_packer_command(command)
    if not packed.is_file() or packed.stat().st_size <= 0:
        raise SystemExit(f"[orion-pack] REFUSED: packer produced no packed payload: {packed}")

    # Stage the bootstrap AS OrionNative.exe (overwrites the un-packed app).
    _verify_stageable(bootstrap_exe, "LetheShardBootstrap.exe")
    shutil.copy2(bootstrap_exe, app)
    # Stage the unpacked broker.
    _verify_stageable(broker_exe, f"{SHARD_BROKER_NAME} (activation broker)")
    shutil.copy2(broker_exe, package_dir / SHARD_BROKER_NAME)

    return {
        "packed_payload": SHARD_PACKED_PAYLOAD_NAME,
        "bootstrap_source": str(Path(bootstrap_exe).resolve()),
        "broker_source": str(Path(broker_exe).resolve()),
        "packed_size": packed.stat().st_size,
    }


def discover_shard_support_files(package_dir: Path) -> list[str]:
    """Qt plugins the broker needs + any VC runtime deps actually present (must be covered)."""
    support: list[str] = []
    for rel in ("platforms/qwindows.dll", "tls/qschannelbackend.dll"):
        if (package_dir / rel).is_file():
            support.append(rel)
    for path in sorted(package_dir.glob("*.dll")):
        lower = path.name.lower()
        if lower.startswith(("msvcp140", "vcruntime140", "concrt140", "vccorlib140")):
            support.append(path.name)
    return support


def assert_server_shard_coverage(package_dir: Path) -> None:
    """FAIL CLOSED unless every shard essential is present AND manifest-covered (FIX #1)."""
    manifest_path = package_dir / "release_manifest.json"
    try:
        files = json.loads(manifest_path.read_text(encoding="utf-8")).get("files") or {}
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"[orion-pack] REFUSED: shard manifest unreadable: {exc}") from exc

    required = list(SERVER_SHARD_REQUIRED_FILES) + discover_shard_support_files(package_dir)
    missing = sorted({rel for rel in required if not (package_dir / rel).is_file()})
    uncovered = sorted({rel for rel in required
                        if (package_dir / rel).is_file() and rel not in files})
    if missing or uncovered:
        raise SystemExit(
            "[orion-pack] REFUSED (server-shard fail-closed): shard chain incomplete.\n"
            f"  missing on disk: {missing or 'none'}\n"
            f"  present but NOT manifest-covered: {uncovered or 'none'}"
        )


def validate_server_shard_release(package_dir: Path, *, public_key_b64: str = "",
                                  key_id: str | None = None) -> None:
    """The server-shard gate: essentials + coverage + full release integrity, fail-closed."""
    key_id = key_id or _pkg().ED25519_KEY_ID
    assert_server_shard_coverage(package_dir)
    result = _vri().verify(package_dir, public_key_b64, key_id)
    if not result.get("ok"):
        raise SystemExit(
            "[orion-pack] REFUSED (server-shard fail-closed): assembled package fails release "
            "integrity: " + "; ".join(result.get("errors", [])))


def validate_server_shard_release_mode(enabled: bool, stage: Path | None = None, *,
                                       public_key_b64: str = "",
                                       key_id: str | None = None) -> None:
    """Server-shard gate entry point (FAIL-CLOSED; VERIFIES, never merely un-blocks).

    * enabled=False -> no-op (non-shard path unchanged).
    * enabled=True, stage=None -> the assembled/verified chain has not been produced yet,
      so refuse (requirements not met).
    * enabled=True, stage given -> verify the three-exe chain + every shard essential is
      present and covered by the signed customer manifest; raise on any gap.
    """
    if not enabled:
        return None
    if stage is None:
        raise ValueError(SERVER_SHARD_RELEASE_BLOCKER)
    validate_server_shard_release(stage, public_key_b64=public_key_b64, key_id=key_id)
    return None


# ── customer-package acceptance (FIX #3 round 2) ─────────────────────────────────

def assert_customer_package_acceptance(package_dir: Path, *, server_shard: bool = False) -> None:
    """The FULL customer acceptance test for an assembled/received package.

    A green ``verify_release_integrity.verify()`` proves only that every manifested
    file hashes correctly under a signature from the pinned key. It records an
    UNMANIFESTED file as a *warning*, so on its own it accepts:
      * a package carrying an extra unmanifested DLL/EXE (AGENTS.md: a release blocker),
      * an INTERNAL-audience package still carrying Owner/Staff,
      * a correctly signed but semantically unsafe ``security_policy.json``,
      * an executable that is not admitted for this package profile.

    This gate closes all four and fails closed. It NEVER writes to *package_dir*.
    """
    package_dir = Path(package_dir)
    audit = _audit()
    problems: list[str] = []

    # 1. Audience must be the customer manifest, not an internal one.
    manifest_path = package_dir / "release_manifest.json"
    manifest: dict = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        problems.append(f"release manifest unreadable: {exc}")
    audience = manifest.get("audience")
    if audience != "customer":
        problems.append(
            f"manifest audience is {audience!r}, not 'customer' — an internal package "
            "must never be published to customers")

    # 2. Owner/Staff absent from BOTH the disk and the manifest.
    covered = manifest.get("files") or {}
    for name in INTERNAL_ADMIN_BINARIES:
        if (package_dir / name).is_file():
            problems.append(f"internal admin binary present on disk: {name}")
        if name in covered:
            problems.append(f"internal admin binary listed in the customer manifest: {name}")

    # 3+4+5. Zero unmanifested files, full security_policy.json semantics, and
    # profile-aware executable admission — the independent audit, run in-process.
    findings = list(audit.audit_package_manifest(package_dir))
    findings.extend(audit.audit_executable_admission(package_dir, server_shard=server_shard))
    problems.extend(finding.format() for finding in findings)

    if problems:
        raise SystemExit(
            "[orion-pack] REFUSED: package fails CUSTOMER acceptance (integrity alone is not "
            "sufficient):\n  - " + "\n  - ".join(problems))
    print(f"[orion-pack] customer acceptance OK (audience=customer, 0 unmanifested files, "
          f"no Owner/Staff, policy fails closed, executables admitted"
          f"{' [server-shard profile]' if server_shard else ''})")


# ── verify-only (FIX #3) ─────────────────────────────────────────────────────────

def verify_only(input_dir: Path, *, server_shard: bool = False,
                public_key_b64: str = "", key_id: str | None = None) -> int:
    """Validate an EXISTING package WITHOUT copying, packing, regenerating, or signing."""
    key_id = key_id or _pkg().ED25519_KEY_ID
    input_dir = input_dir.resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"[orion-pack] REFUSED: --verify-only needs an existing package dir: {input_dir}")
    if not (input_dir / "release_manifest.json").is_file():
        raise SystemExit(
            f"[orion-pack] REFUSED: --verify-only needs a packaged input with a release "
            f"manifest; none at {input_dir}")
    if publish_journal_path().is_file():
        raise SystemExit(
            "[orion-pack] REFUSED: an interrupted publish is pending "
            f"({publish_journal_path()}); the published artifact set may be MIXED. Re-run "
            "the packer (it replays the journal) before verifying.")

    result = _vri().verify(input_dir, public_key_b64, key_id)
    if not result.get("ok"):
        raise SystemExit(
            "[orion-pack] FAIL: input package fails release integrity (manifest/signature/hash):\n  - "
            + "\n  - ".join(result.get("errors", [])))
    print(f"[orion-pack] verify-only: release integrity OK ({result.get('checked', 0)} files, "
          "manifest signature verified against the pinned key)")

    # Integrity is NOT acceptance: unmanifested runtime files, an internal-audience
    # manifest, a semantically unsafe policy, or an unadmitted executable must all fail
    # here without changing a single input byte.
    assert_customer_package_acceptance(input_dir, server_shard=server_shard)

    if server_shard:
        # A shard package verified read-only must still satisfy the chain gate.
        validate_server_shard_release(input_dir, public_key_b64=public_key_b64, key_id=key_id)
        print("[orion-pack] verify-only: server-shard chain present + covered")

    extra = (SHARD_BROKER_NAME,) if (input_dir / SHARD_BROKER_NAME).is_file() else ()
    customer_startup_smoke(input_dir, extra_binaries=extra,
                           probe=_pinned_probe(public_key_b64, key_id))
    tamper_refusal_smoke(input_dir, server_shard=server_shard,
                         probe=_pinned_probe(public_key_b64, key_id))
    print("[orion-pack] verify-only OK (no copy, no packer, no re-sign)")
    return 0


# ── report (FIX #7 + #10) ────────────────────────────────────────────────────────

def build_report(*, mode: str, input_dir: Path, output_dir: Path, targets: list[dict],
                 provenance: dict | None, process_hardening: bool, server_shard: bool,
                 shard: dict | None, production: bool = True) -> dict:
    return {
        # v3 adds "production" (round 2): a non-production build is stamped, never guessed.
        "schema": "orion.packing_report.v3",
        "mode": mode,
        # False only for an explicitly-marked --non-production-build (non-production key,
        # relaxed packer pin). A shippable artifact set is always production=true.
        "production": production,
        "input_package": str(input_dir),
        "output_package": str(output_dir),
        "packer": {
            "tool": "lethe",
            "process_hardening": process_hardening,
            "server_shard": server_shard,
            # NEVER the raw command template or any secret (FIX #10). Secrets are
            # injected via the environment/secret manager; the template is redacted.
            "command_template": "(redacted — secrets injected via env; see docs/PACKING_RUNBOOK.md)",
            "provenance": provenance,
            "shard": shard,
        },
        "targets": targets,
        "note": "Report is intentionally outside the shipped package; it records no secret "
                "and no raw packer command line.",
    }


# ── default packer command builders (FIX #9) ─────────────────────────────────────

def _resolve_default_packer_command(server_shard: bool, shard_url: str) -> str | None:
    lethe_cli = Path(DEFAULT_LETHE_CLI)
    if not lethe_cli.is_file():
        return None
    # FIX #9: enable Lethe's DLL-search-order hardening (restricted default DLL
    # directories) in the default pack command.
    base = f'"{sys.executable}" "{lethe_cli}" {{input}} {{output}} --process-hardening'
    if server_shard:
        base += (
            " --server-shard --enable-experimental-server-shard "
            f'--shard-url "{shard_url}"'
        )
    return base


# ── main ─────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Strict release package to copy from.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Packed package output directory.")
    parser.add_argument("--targets", default=",".join(DEFAULT_TARGETS),
                        help="Comma-separated package-relative EXE targets (production is EXE-only).")
    parser.add_argument("--packer-command", help="External packer command template ({input}/{output}).")
    parser.add_argument("--verify-only", action="store_true",
                        help="READ-ONLY: validate an existing package against the pinned key; "
                             "never copy/pack/regenerate/sign.")
    parser.add_argument("--no-pack", action="store_true",
                        help="Assembly mode WITHOUT running the packer (re-sign/assemble the "
                             "input into the customer package; explicit re-signing mode).")
    parser.add_argument("--signing-key", default=os.environ.get("ORION_UPDATE_SIGNING_KEY_PEM", ""),
                        help="Ed25519 private key PEM used to sign the packed customer manifest.")
    parser.add_argument("--require-live-backend", action="store_true",
                        help="Also require live owner/staff route contract to pass.")
    parser.add_argument("--backend-url", default="https://api.zaeorion.com")
    parser.add_argument("--server-shard", action="store_true",
                        help="Assemble the server-shard three-exe activation chain (fail-closed "
                             "until the chain + essentials are present and manifest-covered).")
    parser.add_argument("--bootstrap-exe", type=Path, default=Path(DEFAULT_BOOTSTRAP_EXE),
                        help="LetheShardBootstrap.exe staged AS OrionNative.exe (server-shard).")
    parser.add_argument("--broker-exe", type=Path, default=Path(DEFAULT_BROKER_EXE),
                        help="Unpacked OrionActivate.exe broker (server-shard).")
    parser.add_argument("--shard-url", default=DEFAULT_SHARD_URL, help="Shard gate base URL.")
    parser.add_argument("--lethe-root", type=Path, default=Path(DEFAULT_LETHE_ROOT),
                        help="Lethe packer repo root (for provenance).")
    parser.add_argument("--expected-packer-commit", default="",
                        help="Pin the packer HEAD to an exact commit (owner step).")
    parser.add_argument("--allow-dirty-packer", action="store_true",
                        help="Allow a dirty packer tree. Requires --non-production-build: a "
                             "production-signed release may never come from an unversioned tree.")
    parser.add_argument("--non-production-build", action="store_true",
                        help="Explicitly mark this run as NON-production: the production signing "
                             "key is REFUSED, the packer-commit pin is optional, and "
                             "--allow-dirty-packer becomes available. Output is stamped "
                             "production=false and must never be shipped.")
    parser.add_argument("--skip-archive", action="store_true",
                        help="Skip the zip + update manifest (package dir only).")
    args = parser.parse_args(argv)

    # Target validation runs early in EVERY mode so a DLL target fails before any copy.
    try:
        targets = parse_targets(args.targets)
        validate_release_targets(targets)
    except ValueError as exc:
        parser.error(str(exc))

    # ---- verify-only: read-only, no key required, no mutation (FIX #3) ----
    if args.verify_only:
        return verify_only(args.input, server_shard=args.server_shard)

    # ---- assembly (pack / re-sign / server-shard) ----
    process_hardening = False  # set true only when a packer command enables it (FIX #9)
    packer_command = args.packer_command
    if not args.no_pack and not packer_command:
        packer_command = _resolve_default_packer_command(args.server_shard, args.shard_url)
        if packer_command is None:
            parser.error(
                "--packer-command is required unless --verify-only/--no-pack is set "
                f"(Lethe CLI not found at {DEFAULT_LETHE_CLI}; set LETHE_CLI or pass --packer-command)")
        print(f"[orion-pack] no --packer-command given; defaulting to Lethe CLI with "
              "--process-hardening (restricted DLL search dirs)")
    if packer_command:
        try:
            assert_no_credential_bearing_args(packer_command)          # FIX #10
            format_packer_command(packer_command, Path("in.bin"), Path("out.bin"))
        except ValueError as exc:
            parser.error(str(exc))
        process_hardening = "--process-hardening" in packer_command

    if not args.signing_key:
        parser.error("--signing-key or ORION_UPDATE_SIGNING_KEY_PEM is required; packed output may not be unsigned")
    signing_key = Path(args.signing_key).expanduser().resolve()
    pkg = _pkg()

    # FIX #5 round 2: production provenance is not bypassable. A production run (the
    # trusted signing key, the default) REQUIRES an exact packer-commit pin and refuses
    # a dirty packer tree outright. Relaxations live only behind --non-production-build,
    # which in turn REFUSES the production key, so a dirty/unpinned packer tree can never
    # produce production-signed, publishable output.
    production = not args.non_production_build
    if production:
        # Flag contract first, so the refusal names the missing pin rather than the key.
        if args.allow_dirty_packer:
            parser.error(
                "--allow-dirty-packer is refused for a production-signed release; pass "
                "--non-production-build (which refuses the production signing key) for a test build")
        if packer_command and not args.expected_packer_commit:
            parser.error(
                "a production-signed release must PIN the packer: pass --expected-packer-commit "
                "<clean lethe commit> (or --non-production-build for a test build)")
        pkg.require_trusted_production_signing_key(signing_key, package_dir=args.output.resolve())
    else:
        if pkg.is_trusted_production_signing_key(signing_key, package_dir=args.output.resolve()):
            parser.error(
                "--non-production-build must not be signed with the trusted PRODUCTION key; "
                "use a throwaway test key, or drop the flag and pin a clean packer commit")
        print("[orion-pack] WARNING: --non-production-build — output is NOT production "
              "(non-production key; pin/dirty-tree checks relaxed). Never ship this artifact.")

    provenance = None
    if packer_command:
        provenance = collect_packer_provenance(               # FIX #7
            args.lethe_root, expected_commit=args.expected_packer_commit,
            allow_dirty=args.allow_dirty_packer)

    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    # FIX #7 round 2: finish any promotion a previous run was KILLED in the middle of,
    # so this run never builds on top of a half-published artifact set. A crash
    # can leave the default input name temporarily absent until this recovery
    # restores it; checking existence first would prevent recovery altogether.
    recover_publish_journal(RELEASE_DIR)
    if not input_dir.is_dir():
        parser.error(f"input package does not exist: {input_dir}")

    staging_root = create_pack_staging_root(output_dir)
    stage_pkg = staging_root / output_dir.name
    shard_info = None
    packed_targets: list[dict] = []
    try:
        copy_package(input_dir, stage_pkg)
        version = _manifest_version(stage_pkg)

        # 1. Pack / shard-assemble.
        if args.server_shard:
            shard_info = stage_server_shard_chain(
                stage_pkg, command_template=packer_command,
                bootstrap_exe=args.bootstrap_exe,
                broker_exe=args.broker_exe)
        elif packer_command:
            packed_targets = run_packer(stage_pkg, targets, packer_command)

        # 2. INTERNAL stage: cover Owner/Staff, smoke them, THEN strip (FIX #4).
        write_internal_manifest(stage_pkg, signing_key, version)
        internal_admin_smoke(stage_pkg, runner=_default_startup_runner)
        pkg.strip_customer_excluded_files(stage_pkg)

        # 3. CUSTOMER stage: sign the final customer manifest, then audit + verify.
        write_customer_manifest(stage_pkg, signing_key, version)
        if args.server_shard:
            assert_server_shard_coverage(stage_pkg)
        audit_package(stage_pkg, server_shard=args.server_shard)
        assert_customer_package_acceptance(stage_pkg, server_shard=args.server_shard)
        validate_server_shard_release_mode(args.server_shard, stage_pkg)  # gate (fail-closed)

        result = _vri().verify(stage_pkg, "", pkg.ED25519_KEY_ID)
        if not result.get("ok"):
            raise SystemExit(
                "[orion-pack] FAIL: assembled customer package fails release integrity:\n  - "
                + "\n  - ".join(result.get("errors", [])))

        # 4. CUSTOMER smokes + tamper refusal (FIX #6).
        extra = (SHARD_BROKER_NAME,) if args.server_shard else ()
        customer_startup_smoke(stage_pkg, extra_binaries=extra)
        tamper_refusal_smoke(stage_pkg, server_shard=args.server_shard)

        if args.require_live_backend:
            run([sys.executable, "tools/admin/check_backend_contract.py", "--base-url", args.backend_url])

        # 5. Build the complete artifact set in staging, then promote atomically (FIX #5).
        staged_items: list[tuple[Path, Path]] = [(stage_pkg, output_dir)]
        report = build_report(
            mode="server-shard" if args.server_shard else ("re-sign" if args.no_pack else "pack"),
            input_dir=input_dir, output_dir=output_dir, targets=packed_targets,
            provenance=provenance, process_hardening=process_hardening,
            server_shard=args.server_shard, shard=shard_info, production=production)
        staged_report = staging_root / "orion-packing-report.json"
        staged_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        staged_items.append((staged_report, RELEASE_DIR / "orion-packing-report.json"))

        if not args.skip_archive:
            pack_version = version or pkg.default_version()
            zip_path, digest = pkg.create_archive(
                stage_pkg, pack_version, out_dir=staging_root)
            # Distinct name so the PACKED artifact never overwrites the unpacked
            # orion-package-<v>.zip that tools/package_orion_release.py emits.
            packed_zip = staging_root / f"orion-package-packed-{pack_version}.zip"
            zip_path.replace(packed_zip)
            digest = sha256_file(packed_zip)
            update_manifest = pkg.build_update_manifest(version=pack_version, artifact_sha256=digest)
            update_manifest = pkg.sign_update_manifest(update_manifest, signing_key)
            staged_manifest = staging_root / "update_manifest.json"
            staged_manifest.write_text(json.dumps(update_manifest, indent=2), encoding="utf-8")
            staged_items.append((packed_zip, RELEASE_DIR / packed_zip.name))
            staged_items.append((staged_manifest, RELEASE_DIR / "update_manifest.json"))

        promote_pack_output(staged_items, PACK_ARCHIVE_DIR, RELEASE_DIR)
    finally:
        if staging_root.exists():
            cleanup_pack_staging_root(staging_root, output_dir)

    print(f"[orion-pack] wrote {output_dir}")
    print("[orion-pack] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
