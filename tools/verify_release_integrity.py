"""verify_release_integrity.py — CI/pre-ship gate that replicates the NATIVE integrity check.

Runs the SAME pass/fail logic as native SecurityManager::verifyReleaseIntegrity()
(native_orion/src/SecurityManager.cpp:355) against a packaged build, so a green run here means a
PRODUCTION build will NOT fail closed on startup (in prod releaseManifestRequired() is hard-true and
a bad/missing manifest LOCKS the app). Also runs the forbidden-file scan so no secret/key/dev
artifact ships. Exit 0 = shippable; exit 1 = would lock or leaks a forbidden file.

Usage:
  python tools\\verify_release_integrity.py --package release\\orion-package
  python tools\\verify_release_integrity.py --package <dir> --json out.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import posixpath
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# Reuse the packager's sha256 + forbidden-file scan so this gate can never drift from what actually
# ships. Loaded by path (tools/ is not a package).
_spec = importlib.util.spec_from_file_location(
    "package_orion_release", _ROOT / "tools" / "package_orion_release.py"
)
_pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pkg)

MANIFEST_SCHEMA = "orion.release_manifest.v1"


def looks_safe_manifest_path(rel: str) -> bool:
    """Port of SecurityManager.cpp looksSafeManifestPath(): relative, no traversal."""
    clean = posixpath.normpath(rel.strip().replace("\\", "/"))
    if not clean or clean == "..":
        return False
    if os.path.isabs(clean) or clean.startswith("/") or (len(clean) > 1 and clean[1] == ":"):
        return False
    if clean.startswith("../") or "/../" in clean:
        return False
    return True


def _expected_hash(value) -> str | None:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, dict):
        h = value.get("sha256")
        return h.strip().lower() if isinstance(h, str) else None
    return None


def _verify_release_manifest_signature(manifest_bytes: bytes, manifest: dict,
                                       signature_path: Path,
                                       public_key_b64: str,
                                       expected_key_id: str) -> list[str]:
    """Verify Ed25519 over the exact on-disk manifest bytes."""
    errors: list[str] = []
    if manifest.get("signature_required") is not True:
        errors.append("Release manifest signature_required must be true")
    if str(manifest.get("signature_alg", "")).strip().lower() != "ed25519":
        errors.append("Release manifest signature_alg must be ed25519")
    key_id = str(manifest.get("public_key_id", "")).strip()
    if key_id != expected_key_id:
        errors.append(
            f"Release manifest public_key_id is not trusted (got {key_id!r}, expected {expected_key_id!r})"
        )
    if not signature_path.is_file():
        errors.append("Release manifest signature missing")
        return errors
    try:
        encoded = signature_path.read_bytes()
    except OSError as exc:
        errors.append(f"Release manifest signature unreadable: {exc}")
        return errors
    if len(encoded) > 4096:
        errors.append("Release manifest signature exceeds 4096-byte limit")
        return errors
    try:
        signature_text = encoded.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        errors.append("Release manifest signature is not ASCII")
        return errors
    signature = _pkg.decode_signature_bytes(signature_text)
    if len(signature) != 64:
        errors.append("Release manifest signature is invalid/undecodable")
        return errors
    public_key = _pkg.decode_ed25519_public_key(public_key_b64)
    if len(public_key) != 32:
        errors.append("Release manifest trusted Ed25519 public key is invalid")
        return errors
    if errors:
        return errors
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        errors.append(f"Release manifest verifier unavailable: {exc}")
        return errors
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, manifest_bytes)
    except InvalidSignature:
        errors.append("Release manifest Ed25519 signature INVALID")
    except ValueError as exc:
        errors.append(f"Release manifest verifier unavailable: {exc}")
    return errors


def verify(package_dir: Path, public_key_b64: str = "",
           expected_key_id: str = _pkg.ED25519_KEY_ID) -> dict:
    """Return {ok, errors, warnings, checked}. `errors` non-empty == the native verifier would LOCK."""
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = package_dir / "release_manifest.json"
    signature_path = package_dir / "release_manifest.sig"

    if not manifest_path.exists():
        return {"ok": False, "errors": ["Release manifest missing"], "warnings": [], "checked": 0}

    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "errors": [f"Release manifest is invalid JSON: {exc}"], "warnings": [], "checked": 0}

    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"Release manifest schema mismatch (got {manifest.get('schema')!r})")
    errors.extend(_verify_release_manifest_signature(
        manifest_bytes,
        manifest,
        signature_path,
        public_key_b64 or _pkg.ED25519_PUBLIC_KEY_B64,
        expected_key_id,
    ))
    files = manifest.get("files") or {}
    if not isinstance(files, dict) or not files:
        errors.append("Release manifest contains no files")
        return {"ok": False, "errors": errors, "warnings": warnings, "checked": 0}

    listed: set[str] = set()
    for rel, value in files.items():
        rel_posix = rel.replace("\\", "/")
        listed.add(posixpath.normpath(rel_posix))
        if not looks_safe_manifest_path(rel):
            errors.append(f"Unsafe manifest path: {rel}")
            continue
        expected = _expected_hash(value)
        if not expected or len(expected) != 64:
            errors.append(f"Invalid hash for {rel}")
            continue
        candidate = package_dir / rel_posix
        if not candidate.exists():
            errors.append(f"Manifest file missing: {rel}")
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"Hash mismatch: {rel}")

    # Advisory (native does NOT check this, so it's a warning, not a lock): files present in the
    # package but absent from the manifest — a file that slipped past packaging is unverified.
    for path in package_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = posixpath.normpath(path.relative_to(package_dir).as_posix())
        if rel in ("release_manifest.json", "release_manifest.sig"):
            continue
        if rel not in listed:
            warnings.append(f"Unlisted (unverified) file in package: {rel}")

    # Forbidden-file scan — the same guard packaging uses; a secret/key/dev artifact here is a HARD fail.
    for finding in _pkg.scan_forbidden(package_dir):
        errors.append(f"Forbidden file in package: {finding}")

    # Secret-CONTENT scan (AWS keys, bot tokens, PEM private-key blocks, dev keys pasted into
    # otherwise-legitimate shipped files) — same pattern set as tools/security_audit.py.
    for finding in _pkg.scan_secret_content(package_dir):
        errors.append(f"Secret content in package: {finding}")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "checked": len(files)}


def verify_update_manifest(manifest_path: Path, artifact_path: Path | None = None,
                           public_key_b64: str = "") -> dict:
    """Replicates the NATIVE updater's acceptance test (UpdateManifest.cpp
    verifyManifestSignature + verifyArtifactSha256): Ed25519 over the canonical
    MANIFEST_SIGN_FIELDS payload, then the artifact hash. A pass here means a shipped
    client WILL accept this manifest/artifact pair; any error means the rollout is dead
    on arrival (or unsigned)."""
    errors: list[str] = []
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "errors": [f"Update manifest unreadable: {exc}"]}

    for field in _pkg.MANIFEST_SIGN_FIELDS:
        if field not in manifest:
            errors.append(f"Update manifest missing signed field: {field}")
    if errors:
        return {"ok": False, "errors": errors}

    if str(manifest.get("signature_alg", "")).lower() != "ed25519":
        errors.append(f"Update manifest signature_alg must be ed25519 (got {manifest.get('signature_alg')!r})")
    if not str(manifest.get("latest_version", "")).strip():
        errors.append("Update manifest latest_version is empty")

    sha = str(manifest.get("sha256", "")).strip().lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        errors.append("Update manifest sha256 is not 64 hex chars")

    sig = _pkg.decode_signature_bytes(str(manifest.get("signature", "")))
    if not sig:
        errors.append("Update manifest signature missing/undecodable (unsigned manifests never ship)")

    pub_raw = _pkg.decode_ed25519_public_key(public_key_b64 or str(manifest.get("public_key_b64", "")))
    if not pub_raw:
        errors.append("No usable Ed25519 public key (pass --public-key or embed public_key_b64)")

    if sig and pub_raw:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, _pkg.canonical_signing_payload(manifest))
        except InvalidSignature:
            errors.append("Update manifest Ed25519 signature INVALID (native client would reject)")

    if artifact_path is not None:
        artifact_path = Path(artifact_path)
        if not artifact_path.is_file():
            errors.append(f"Artifact missing: {artifact_path}")
        elif len(sha) == 64:
            actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            if actual != sha:
                errors.append(f"Artifact sha256 mismatch: manifest={sha} actual={actual}")

    return {"ok": not errors, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True, help="Packaged build dir (contains release_manifest.json)")
    ap.add_argument("--json", default="", help="Also write the full result as JSON here")
    ap.add_argument("--strict-warnings", action="store_true", help="Treat unlisted-file warnings as failures")
    ap.add_argument("--update-manifest", default="", help="Signed /api/update manifest JSON to verify (Ed25519)")
    ap.add_argument("--artifact", default="", help="Zip artifact to check against the update manifest sha256")
    ap.add_argument("--public-key", default="", help="Ed25519 public key (b64/hex) overriding public_key_b64")
    ap.add_argument("--release-public-key", default="",
                    help="Trusted release-manifest Ed25519 public key override (tests/key rotation)")
    ap.add_argument("--release-key-id", default=_pkg.ED25519_KEY_ID,
                    help="Expected trusted release-manifest public_key_id")
    args = ap.parse_args()

    package_dir = Path(args.package).resolve()
    if not package_dir.is_dir():
        print(f"[verify] not a directory: {package_dir}", file=sys.stderr)
        return 2

    result = verify(package_dir, args.release_public_key, args.release_key_id)

    if args.update_manifest:
        um = verify_update_manifest(
            Path(args.update_manifest),
            Path(args.artifact) if args.artifact else None,
            args.public_key,
        )
        result["update_manifest_ok"] = um["ok"]
        result["errors"].extend(f"[update-manifest] {e}" for e in um["errors"])
        result["ok"] = result["ok"] and um["ok"]
    print(f"[verify] {package_dir}")
    print(f"[verify] files checked: {result['checked']}")
    for w in result["warnings"]:
        print(f"  WARN  {w}")
    for e in result["errors"]:
        print(f"  FAIL  {e}")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")

    failed = (not result["ok"]) or (args.strict_warnings and result["warnings"])
    if failed:
        print(f"[verify] RESULT: FAIL — this package would lock a production build "
              f"({len(result['errors'])} errors, {len(result['warnings'])} warnings)")
        return 1
    print(f"[verify] RESULT: PASS — integrity chain intact ({result['checked']} files verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
