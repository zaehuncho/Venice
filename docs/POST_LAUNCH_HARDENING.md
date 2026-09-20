# Post-launch hardening — v1.0.1 checklist

Written 2026-08-06 night, before Sunday's ship. Everything here is a **known,
measured** gap that we ship with deliberately, because closing it Sunday morning
carries more risk than the gap itself.

Each item lists: what's exposed, why we shipped anyway, and the fix.

---

## 1. SmartScreen warning on first install

**Exposed:** unsigned installer triggers "Windows protected your PC" on every
buyer's first install. They must click *More info → Run anyway*. Support-ticket
and conversion cost.

**Why we shipped:** Azure Trusted Signing enrollment has a hard 3-day account-age
wall plus identity validation — not reachable by Sunday even if started
immediately.

**Fix (v1.0.1, Mon–Wed):**
1. Enroll in Azure Trusted Signing (~$10/mo, no hardware token needed).
2. Add `-SignToolCommand` to `installer/build_installer.ps1`.
3. Signed installer + signed EXEs → clean install from day one.
4. Existing installs auto-upgrade via the already-working manifest signing.

---

## 2. DLL plaintext exposure

**Exposed:** static string audit found ~250 business-relevant plaintext strings
across the four core DLLs, including full C++ method signatures via MSVC
name-mangling. An attacker with the DLL and IDA Pro can reconstruct class
layouts and method maps.

Measured 2026-08-06 (`release/orion-package/`):

| DLL | Total strings ≥6 chars | Business-relevant |
|---|---:|---:|
| AutomationCore.dll | 1,332 | 137 (flag names, gate reasons, algorithm hints) |
| SecurityCore.dll | 813 | 85 (LicenseClient methods, cache format markers) |
| VisionCore.dll | 308 | 35 (MeterDetector method signatures) |
| RemotePlayCore.dll | 1,570 | 17 (log strings, mangled names) |

**Why we shipped:**
- The on-device threat is bounded. An attacker with the DLLs can *read* internals
  offline but cannot *run* the bot: `SecurityCore` gates automation on a live
  backend-issued lease (`LeaseGate.cpp`), the entitlement cache is DPAPI-bound
  to the machine/user/build (`SecurityManager.cpp`), and the release manifest
  hash is verified before automation starts.
- Lethe's DLL path is blocked by `pack_lethe_release.py`: packed DLLs
  currently unpack from `DllMain`, which is a real loader-lock risk. Fixing that
  is a design change, not a config flag.
- MSVC hardening flags (CFG, CET, ASLR, LTCG, dead-code folding) are already on
  all four DLLs — they defend against *exploit chains*, not static reversing.

**Fix (v1.0.1, ~½ day per the Lethe repo's `PHASE2_PHASE3_PLAN.md` §2C — Lethe is now a standalone repo at `C:\Users\aaron\Desktop\Lethe`):**
1. Add `/GR-` to SecurityCore compile flags — strips RTTI type_info structures
   that contain plaintext class names. Verify no `dynamic_cast`/`typeid` calls
   in SecurityCore (there aren't any — it's signal/slot only).
2. Audit `OrionExports.h` and reduce SecurityCore's dllexport surface to the
   minimum symbols `OrionNative.exe` actually imports.
3. Add `/EMITPOGODB:NO` to the linker to prevent PGO database embedding.
4. Consider wiring the existing `obfs_string.h` machinery into the highest-value
   string literals in `AutomationEngine.cpp` and `SecurityManager.cpp`. This
   exists but is currently used in zero source files; needs its own careful
   rollout, not a mass find-replace.

Longer-term: implement the deferred-init pattern
`pack_lethe_release.py:validate_release_targets` is waiting for, so Lethe
can protect DLLs the same way it protects EXEs.

---

## 3. Sidecar Python code exposure

**Exposed:** `OrionSidecar.exe` is a Nuitka native compile of the Python sidecar.
Comments and Python source are gone (Nuitka compiles to C then to a native PE),
but strings and constant tables inside the compiled code are recoverable with
work.

**Why we shipped:** RUNBOOK explicitly forbids packing the sidecar: *"Python/Nuitka
bundle with native numpy/cv2 deps; packing corrupts the embedded runtime."*
Verified: not on the packer target list.

**Fix (v1.0.1+):** Move the highest-value Python logic to native C++ where it
gets the same protections as the other DLLs. Or wait until the deferred-init
DLL packing lands and package the sidecar's C output as a DLL through Lethe.

---

## 4. What's already protected, for the record

Not a gap — a reminder of what's already working so we don't over-scope v1.0.1:

- Ed25519 update manifest signing (verified against pinned trust anchor).
- Release manifest hash gate — `SecurityManager` refuses to arm automation if
  any packaged file's SHA-256 doesn't match `release_manifest.json`.
- DPAPI-bound entitlement cache (machine ID + user + build SHA-256).
- Backend `/api/activate` with per-machine trial guard, per-license machine
  count, and lease expiry.
- MSVC CFG + CET + ASLR + LTCG + dead-code folding on all four DLLs.
- Lethe section encryption (AES-256-GCM) on all four shipping EXEs, with
  code-hash-bound key and import elision — verified 2026-08-06:
  entropy 6.0 → 7.6, zero plaintext leaks on 3-probe redteam.
- Anti-debug (gated, AV-clean) on the packed EXEs.
- Windows Authenticode signature verification on VC++ redistributable (Valid,
  Microsoft Corporation).

---

## 5. Risks found in the 2026-08-07 pre-ship audit — DEFERRED

Four parallel agents audited the fresh-install codepath. Five criticals were fixed
this session; the rest are documented here because they carry real risk but are not
observed failures on the shipping build:

### 5a. ViGEmBus/HidHide reboot requirement not surfaced (CRITICAL when it hits)
`installer/orion.iss:131,133,141` — installs the drivers `/quiet /norestart` then
auto-launches Venice. On a machine that never had ViGEmBus, the bus driver is
registered but the kernel driver often needs a reboot to load. First launch shows
"ViGEmBus driver is missing" with no explanation of what to do.
**Fix v1.0.1:** capture the install exit codes; if either driver was actually
installed (vs skipped-as-present), require reboot before auto-launch. Or drop the
auto-launch and add an installer-final page: "Restart your PC before opening Venice."

### 5b. Hardcoded SPKI cert pins — single point of failure worldwide (CRITICAL long-term)
`NetworkSecurity.cpp:40-56` pins 14 SPKI hashes. If Cloudflare rotates to a CA whose
key isn't in this set, or if any pinned cert reaches its expiry, **every installed
Venice worldwide loses automation simultaneously with no recovery path** (the
LeaseGate needs the heartbeat, the heartbeat needs the pinned TLS to reach the API).
**Fix v1.0.1:** widen the SPKI set to all currently-active Cloudflare edge CAs, emit
a client-side "pin expiry" telemetry that fires at 30 days before the earliest pin's
notAfter, and consider a bounded offline-lease TTL (~24h) as a safety net.

### 5c. `libcrypto-3-x64.dll` hash pin fails silently under AV (HIGH)
`Ed25519.cpp:208-247` + CMake pin `ORION_LIBCRYPTO_SHA256`. If Defender touches the
DLL (rare but real), `ed25519Available()` returns false and every downstream security
check fails with a generic manifest error rather than "your AV corrupted our crypto
DLL". Also, both `libcrypto-3.dll` and `libcrypto-3-x64.dll` ship — only the pinned
one works.
**Fix v1.0.1:** verify packaged DLL bytes against the CMake pin at packaging time;
log a specific "libcrypto not verifiable" error rather than the generic message.

### 5d. VC++ redist `/quiet /norestart` — no exit-code check (HIGH)
`installer/orion.iss:137-138` — VC++ install could fail silently on Windows Server
SKUs missing WU. OrionNative then can't load `msvcp140.dll` → silent launch failure,
no error dialog, customer sees nothing happen after "Install complete."
**Fix v1.0.1:** capture the redist exit code (0 = success, 1638 = already installed,
3010 = success with reboot required); anything else surfaces an install-time error.

### 5e. PS5 pairing UX gap (HIGH — customer confusion)
Customer must run `OrionStream.exe` separately to pair their PS5 before Venice can
connect. This step is undocumented; without pairing, `readRegisteredHosts()` returns
empty and the sidecar starts but never connects.
**Fix v1.0.1:** either bundle an in-Venice pairing wizard (surface OrionStream's
pairing UI as a first-run step), or add a hard doc/store-page note "First step:
pair your PS5 via the included OrionStream shortcut before opening Venice."

### 5f. Sidecar preview through telemetry pipe — architectural (MEDIUM)
`autogreen_sidecar.py:_emit_frame_chunks` sends 130KB base64 JPEG through stdout at
60fps × ~25 flushes/frame. Even with 5a's line-buffering fix, this is 1500 flushes/sec
of pipe traffic sharing the same lock as timing telemetry. Under load, preview will
degrade telemetry latency again.
**Fix v1.0.1:** mandate SHM for preview in production builds. If
`ORION_PREVIEW_SHM_MAPPING` is unset, refuse to start the JPEG chunker and drop
preview frames (the detector doesn't need them). Native side: always populate the
SHM env vars, not gated on `eventNotificationsEnabled`.

### 5g. `synchronous updateSecurityStatus()` callers (MEDIUM)
The SHA-256 memoization fixed in 2026-08-07 makes subsequent evals fast, but the
FIRST evaluation on cold NTFS is still ~seconds. Callers at
`OrionAppController.cpp:1940, 3663, 4172, 5947, 10986` still run on the GUI thread.
The existing `periodicSecurityEvaluator_` handles the periodic path off-thread; the
synchronous callers should optionally route through it when the cache is cold.
**Fix v1.0.1:** add a `updateSecurityStatusAsync()` variant that dispatches through
the evaluator worker with a pessimistic-disarm bridge until the result lands.
