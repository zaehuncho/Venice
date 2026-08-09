# Orion red-team findings (2026-06-20)

Static + sandbox security review of the shippable bundle (`redteam_sandbox/` refreshed to the current
build). Threat model: a local non-admin attacker, a reverse-engineer/pirate, and a tamperer.

## Already secure (verified — no action)
- **TLS cert pinning** — enforced at every Admin/License call site, fail-closed, double-checked.
- **NVDEV dev-bypass** — compile-time `false` under `ORION_PRODUCTION_BUILD` (cannot exist in a ship build).
- **Staff token** — in-memory only, never logged or persisted.
- **IPC named pipes** (input bridge + frame export) — protected DACL `D:P` granting only LocalSystem,
  Administrators, and the interactive user's SID. No cross-user connect → no input-injection / frame-exfil.
- **Packager hygiene** — excludes `.vault`, `settings.json(.sig)`, `master.key`, `license_cache.enc`,
  `learning.json`, logs, `__pycache__`, and the removed PSN tool.
- **Manifest path-traversal** — guarded by `looksSafeManifestPath`.

## Findings

### F1 — Unauthenticated localhost control socket (MEDIUM, local)
`nexus_svc.py` binds `127.0.0.1:47291` and accepts JSON commands (`set_filter`, `set_court_ip`, …)
with **no authentication** (`nexus_svc.py:417` accept → `_handle_cmd`, no token). The service runs
**elevated** (WinDivert). Any local process can connect and point the elevated capture at an arbitrary
`console_ip` → confused-deputy traffic sniffing. Localhost-only (no remote), and the DoS commands were
already removed, so impact is bounded — but an unauth elevated-service socket is a real surface.
**Fix:** per-session shared token — the service writes a random token to a user-ACL'd / DPAPI file on
start; the client sends it as the first line; the service rejects commands until authed. (Optional
bridge; only exposed while running.)

### F2 — Release manifest is hash-only, not signed (MEDIUM)
`SecurityManager::verifyReleaseManifest` checks each file's SHA256 against `release_manifest.json`, but
nothing **signs** the manifest. An attacker who patches a DLL/`.py` just recomputes that file's hash in
the manifest → verification passes. (settings.json IS signed; the manifest is not.)
**Fix:** sign `release_manifest.json` with the existing Ed25519 pinned key (the updater already does
this) and verify the signature in `SecurityManager` before trusting the hashes. Raises the tamper bar
from "regenerate a JSON" to "patch the signed verifier." (Signing needs the private key at package time
— verify side is code; sign side is CI/maintainer.)

### F3 — Python sidecar ships as plaintext source (MEDIUM-HIGH, IP/tamper)
The packager ships `autogreen_sidecar.py` (+ helpers) as readable, editable source — the timing +
detection IP is exposed and modifiable. **Fix:** obfuscate/compile (pyarmor or Nuitka → native).
**Deferred by design:** `docs/RELEASE_SECURITY.md` Phase 8 correctly defers obfuscation until AFTER
live gameplay sign-off (obfuscated binaries cripple the live-batch debugging that's the next step).
Do this post-sign-off, not now.

### F4 — Admin tools in the customer essential set (LOW-MEDIUM)
`package_orion_release.ESSENTIAL_FILES` includes `OrionOwner.exe` / `OrionStaff.exe`. They're
backend-gated (useless without staff creds), so not directly exploitable, but shipping them to
customers is needless attack surface + info-leak about the admin API. **Fix:** produce the customer
package without the admin tools (separate staff distribution), or confirm the split is intentional.

### Minor — sandbox cruft (not shipped)
`redteam_sandbox/owner_strings.txt` (a PE strings dump) + `Get-PSN-AccountID.bat` are local artifacts;
the packager already forbids the PSN bat. Clean from the sandbox; add `owner_strings.txt` to the
packager's forbidden list as belt-and-suspenders.

## Patch plan (recommended order, post-batch where noted)
1. **F1** socket token — contained, do when the Network bridge is next touched.
2. **F2** manifest signature verify (code now) + signing (CI/maintainer key).
3. **F4** customer-package admin-tool split.
4. **F3** obfuscation — Phase 8, after live sign-off.

The high-risk surfaces (TLS, dev-bypass, IPC pipes, secret packaging) are already solid; the findings
are hardening, not open holes.

---
# Round 2 — post-patch re-attack on the hardened sandbox (2026-06-20)

Patched F1 + F4 in the repo, rebuilt, refreshed the disposable `redteam_sandbox/`, obfuscated the
Python, and re-attacked the compiled launcher/updater/staff tools.

## Patches landed
- **F1 FIXED** ✅ — bridge socket now requires a per-session token (server `nexus_svc.py` + client
  `NetworkBridge.cpp`→`RemotePlayCore.dll`); rebuilt, ctest green, batch path untouched. Committed.
- **F4 FIXED** ✅ — `owner_strings.txt` added to the packager forbidden list; sandbox cruft removed.
- **F2** — still open (manifest signing needs the maintainer's private Ed25519 key = CI step).

## F3 obfuscation — PROVEN, with caveats
- `pyarmor 9.2.4` **works on Python 3.14** and produces genuinely opaque output (encrypted bytecode +
  `pyarmor_runtime.pyd`; no readable source).
- **Trial license caps batch obfuscation** ("out of license") → production needs a **licensed pyarmor**
  or **Nuitka** (free, compiles Python→native; no bytecode to recover).
- **Bypass surface:** the pyarmor runtime can be hooked at import to dump decrypted bytecode (public
  unpackers exist). So obfuscation RAISES the bar; for the most sensitive logic, **Nuitka (native) or
  moving it into the compiled `*Core.dll`s is stronger.**

## Binary attack on the exes — clean
- **No hardcoded secrets** (keys/tokens/creds/private keys) in OrionNative / OrionUpdater / OrionOwner
  / OrionStaff. ✅
- **No endpoint/URL leak** in the exes (the API host lives in a DLL — a known public endpoint, not a
  secret). ✅

## CRITICAL ship-process finding
The sandbox/hand-built Release binaries are **NON-production** — `ORION_PRODUCTION` (CMake) defaults
OFF, so the dev escape-hatches (`localDevAllowed`, `ORION_LOCAL_UI_TEST`, update-gate skip, settings
auto-sign) are **compiled in** (proven: `ORION_LOCAL_UI_TEST` string present in the exe). **Ship builds
MUST be configured `-DORION_PRODUCTION=ON`** (compiles them all out + mandates the manifest).
`package_orion_release.py` already refuses to package a non-production build, so the pipeline enforces
it — but never ship a manually-built Release binary. A true ship red-team should target a
`-DORION_PRODUCTION=ON` build (separate build dir; not the dev build used for live batches).

## Net
No new open exploits in the binaries. The posture is solid: F1/F4 closed; the high-risk surfaces
(TLS pinning, IPC pipe DACLs, staff-token handling, DPAPI cache, dev-bypass gating) remain sound. The
remaining hardening is resource-gated: F2 (signing key), full obfuscation (pyarmor license / Nuitka),
and always building `-DORION_PRODUCTION=ON` for ship.
