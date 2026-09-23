"""End-to-end dry runs of the REAL OrionUpdater.exe (dev build) against signed manifests.

Each case runs the binary headless (QT_QPA_PLATFORM=offscreen) with --manifest-file and a fresh
install dir, then grades <installDir>/orion_updater.log. The artifact step needs HTTPS, so the
positive case stops at "Artifact URL is not HTTPS" ON PURPOSE - that line is only reachable after
the signature verified and the downgrade gate passed, which is what this proves on the real exe.
Negative cases must fail closed BEFORE that line. Nothing here touches the real install.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Repo-relative paths (this file lives in tools/security/updater_e2e/).
from pathlib import Path as _P
_REPO = str(_P(__file__).resolve().parents[3]).replace("\\", "/")
REPO = _REPO
EXE = f"{REPO}/native_orion/build/Release/OrionUpdater.exe"
import os as _os
QT_BIN = _os.environ.get("ORION_QT_BIN", "C:/Users/aaron/Qt/6.8.0/msvc2022_64/bin")
KEY_ID = "orion-e2e-test-key"


def canonical(m):
    # Mirrors UpdateManifest.cpp canonicalManifestSigningString (json.dumps escapes match
    # appendJsonString for plain ASCII fields).
    parts = [
        '{"latest_version":' + json.dumps(m["latest_version"]),
        '"minimum_supported_version":' + json.dumps(m["minimum_supported_version"]),
        '"artifact_url":' + json.dumps(m["artifact_url"]),
        '"sha256":' + json.dumps(m["sha256"]),
        '"published_at":' + json.dumps(m["published_at"]),
        '"mandatory":' + ("true" if m["mandatory"] else "false"),
        '"allow_rollback":' + ("true" if m["allow_rollback"] else "false"),
        '"public_key_id":' + json.dumps(m["public_key_id"]),
    ]
    return (",".join(parts) + "}").encode()


def make_manifest(priv, *, version="1.0.4", url="http://127.0.0.1:1/orion-1.0.4.zip",
                  key_id=KEY_ID, tamper=False):
    m = {
        "latest_version": version,
        "minimum_supported_version": "1.0.0",
        "artifact_url": url,
        "sha256": "a" + "b" * 63,
        "published_at": "2026-09-21T00:00:00Z",
        "mandatory": False,
        "allow_rollback": False,
        "public_key_id": key_id,
        "signature_alg": "ed25519",
        "channel": "stable",
    }
    sig = priv.sign(canonical(m))
    m["signature"] = sig.hex()
    if tamper:
        m["latest_version"] = "1.0.5"          # signed over 1.0.4 -> signature no longer matches
    return m


def run_case(name, manifest, pubkeys_where, pub_b64, env_extra=None, current="1.0.0"):
    work = tempfile.mkdtemp(prefix="orion-upd-e2e-")
    install = os.path.join(work, "install")
    os.makedirs(install)
    keys = {"keys": {KEY_ID: pub_b64}}
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PATH"] = QT_BIN + os.pathsep + os.path.dirname(EXE) + os.pathsep + env.get("PATH", "")
    env.pop("ORION_UPDATE_PUBKEYS", None)
    if pubkeys_where == "install_dir":
        with open(os.path.join(install, "update_pubkeys.json"), "w") as f:
            json.dump(keys, f)
    elif pubkeys_where == "env":
        p = os.path.join(work, "env_keys.json")
        with open(p, "w") as f:
            json.dump(keys, f)
        env["ORION_UPDATE_PUBKEYS"] = p
    elif pubkeys_where == "none":
        pass
    if env_extra:
        env.update(env_extra)
    mpath = os.path.join(work, "manifest.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f)
    args = [EXE, "--manifest-file", mpath, "--install-dir", install, "--current-version", current,
            "--no-relaunch", "--dry-run"]
    t0 = time.time()
    try:
        proc = subprocess.run(args, env=env, capture_output=True, timeout=25)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    log_path = os.path.join(install, "orion_updater.log")
    log = open(log_path, encoding="utf-8", errors="replace").read() if os.path.exists(log_path) else ""
    tail = [l.split(" ", 1)[1] if " " in l else l for l in log.strip().splitlines()][-4:]
    print(f"\n== {name}  (exit={rc}, {time.time()-t0:.1f}s)")
    for l in tail:
        print("   |", l[:150])
    shutil.rmtree(work, ignore_errors=True)
    return log


def expect(log, needle, name):
    ok = needle in log
    print(f"   {'PASS' if ok else 'FAIL'}: expected {needle!r}")
    return ok


priv = Ed25519PrivateKey.generate()
pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
pub_b64 = base64.b64encode(pub_raw).decode()
results = []

log = run_case("valid signature, key from install_dir (dev path), non-HTTPS artifact",
               make_manifest(priv), "install_dir", pub_b64)
results.append(expect(log, "Manifest signature OK", "sig ok"))
results.append(expect(log, "Trust root: install_dir", "source"))
results.append(expect(log, "Artifact URL is not HTTPS", "https gate reached"))

log = run_case("valid signature, key from ORION_UPDATE_PUBKEYS env (dev path)",
               make_manifest(priv), "env", pub_b64)
results.append(expect(log, "Trust root: env", "env source"))
results.append(expect(log, "Artifact URL is not HTTPS", "https gate reached"))

log = run_case("tampered manifest (field changed after signing)",
               make_manifest(priv, tamper=True), "install_dir", pub_b64)
results.append(expect(log, "Manifest signature verification FAILED", "sig fails closed"))

log = run_case("unknown public_key_id (no trusted key anywhere)",
               make_manifest(priv, key_id="nobody-1"), "install_dir", pub_b64)
results.append(expect(log, "No trusted public key for public_key_id 'nobody-1'", "key fails closed"))

log = run_case("no pubkeys file at all",
               make_manifest(priv), "none", pub_b64)
results.append(expect(log, "No trusted public key", "fails closed without a file"))

log = run_case("downgrade offered (manifest 0.9.0 < installed 1.0.0, rollback not allowed)",
               make_manifest(priv, version="0.9.0"), "install_dir", pub_b64)
results.append(expect(log, "is older than installed", "downgrade blocked"))

print("\nRESULT:", "ALL PASS" if all(results) else f"{results.count(False)} FAILED", f"({len(results)} checks)")
sys.exit(0 if all(results) else 1)
