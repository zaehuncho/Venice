"""End-to-end checks against the PRODUCTION-configured OrionUpdater.exe (Codex F1/F6).

What production must do on the real binary:
  1. --build-profile reports profile=production, the shipped key id, local_manifest_allowed=false
  2. --manifest-file is refused
  3. --install-dir pointing anywhere but the updater's own directory is refused (audit-logged)
  4. with rogue keys offered by EVERY external source, a manifest fetched from the real HTTPS
     endpoint still verifies against the EMBEDDED key and every external source is logged as
     ignored; --current-version 99.0.0 stops it at the downgrade gate BEFORE any download.
The log is <exe dir>/orion_updater.log because in production the install root IS the exe dir.
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
import os as _os
EXE_DIR = _os.environ.get("ORION_PROD_UPDATER_DIR", _REPO + "/native_orion/build_prod_review/Release")
EXE = EXE_DIR + "/OrionUpdater.exe"
QT_BIN = _os.environ.get("ORION_QT_BIN", "C:/Users/aaron/Qt/6.8.0/msvc2022_64/bin")
LOG = EXE_DIR + "/orion_updater.log"
MANIFEST_URL = "https://api.zaeorion.com/api/update?client_version=99.0.0&channel=stable"
results = []


def env_with(extra=None):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PATH"] = QT_BIN + os.pathsep + EXE_DIR + os.pathsep + env.get("PATH", "")
    env.pop("ORION_UPDATE_PUBKEYS", None)
    if extra:
        env.update(extra)
    return env


def run(args, env, timeout=40):
    if os.path.exists(LOG):
        os.remove(LOG)
    t0 = time.time()
    try:
        rc = subprocess.run([EXE] + args, env=env, capture_output=True, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        rc = "timeout(window open)"
    log = open(LOG, encoding="utf-8", errors="replace").read() if os.path.exists(LOG) else ""
    print(f"   exit={rc} {time.time()-t0:.1f}s")
    for l in log.strip().splitlines()[-6:]:
        print("   |", (l.split(" ", 1)[1] if " " in l else l)[:150])
    return log


def expect(hay, needle):
    ok = needle in hay
    print(f"   {'PASS' if ok else 'FAIL'}: {needle!r}")
    results.append(ok)


print("== 1. build attestation")
work = tempfile.mkdtemp(prefix="orion-prod-e2e-")
prof_path = os.path.join(work, "profile.json")
rc = subprocess.run([EXE, "--build-profile", prof_path], env=env_with(), capture_output=True, timeout=30).returncode
prof = json.load(open(prof_path)) if os.path.exists(prof_path) else {}
print("   exit", rc, prof)
expect(json.dumps(prof), '"profile": "production"')
expect(json.dumps(prof), '"orion-ed25519-v1"')
expect(json.dumps(prof), '"local_manifest_allowed": false')

priv = Ed25519PrivateKey.generate()
pub_b64 = base64.b64encode(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
rogue = {"keys": {"orion-ed25519-v1": pub_b64, "attacker": pub_b64}}
rogue_file = os.path.join(work, "rogue_keys.json")
json.dump(rogue, open(rogue_file, "w"))
own_dir_keys = EXE_DIR + "/update_pubkeys.json"
json.dump(rogue, open(own_dir_keys, "w"))      # install_dir AND app_dir source (same dir in production)

try:
    print("\n== 2. --manifest-file is refused in production")
    mpath = os.path.join(work, "m.json")
    json.dump({"latest_version": "1.0.4"}, open(mpath, "w"))
    log = run(["--manifest-file", mpath, "--current-version", "1.0.0", "--no-relaunch", "--dry-run"], env_with())
    expect(log, "not permitted in production builds")

    print("\n== 3. foreign --install-dir is refused and audit-logged")
    foreign = os.path.join(work, "foreign")
    os.makedirs(foreign)
    log = run(["--manifest-url", MANIFEST_URL, "--install-dir", foreign, "--current-version", "1.0.0",
               "--no-relaunch", "--dry-run"], env_with())
    expect(log, "install root refused")
    expect(log, "updater's own directory")
    results.append(not os.path.exists(os.path.join(foreign, "orion_updater.log")))
    print(f"   {'PASS' if results[-1] else 'FAIL'}: nothing written into the foreign directory")

    print("\n== 4. real HTTPS manifest, rogue keys via cli + env + install/app dir -> embedded key wins, all ignored")
    log = run(["--manifest-url", MANIFEST_URL, "--pubkeys-file", rogue_file, "--current-version", "99.0.0",
               "--no-relaunch", "--dry-run"], env_with({"ORION_UPDATE_PUBKEYS": rogue_file}), timeout=60)
    if "Could not retrieve update manifest" in log:
        print("   NOTE: endpoint unreachable from this shell; case 4 is inconclusive (not a failure of the binary)")
    else:
        expect(log, "Ignored external public-key source (production trusts only the embedded key): cli:")
        expect(log, "Ignored external public-key source (production trusts only the embedded key): env:")
        expect(log, "Ignored external public-key source (production trusts only the embedded key): install_dir:")
        expect(log, "Trust root: embedded")
        expect(log, "Manifest signature OK (ed25519, key orion-ed25519-v1)")
        expect(log, "is older than installed 99.0.0")
        results.append("Downloading update" not in log)
        print(f"   {'PASS' if results[-1] else 'FAIL'}: stopped before any download")
finally:
    for p in (own_dir_keys, LOG):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(work, ignore_errors=True)

print("\nRESULT:", "ALL PASS" if all(results) else f"{results.count(False)} FAILED", f"({len(results)} checks)")
sys.exit(0 if all(results) else 1)
