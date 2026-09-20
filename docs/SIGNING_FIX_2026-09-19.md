# Signing fix: heartbeat lease + update manifest (2026-09-19)

Scope: launch blockers **B2** (lease signing dead in production) and **B5**'s key half
(`/orion/manifest_signing_key` "does not match the release public key") from
`docs/LAUNCH_LIVE_AUDIT_2026-09-19.md`.

Nothing live was changed by this work: no SSM writes, no Lambda deploy, no Cloudflare or Discord
writes, no git mutation. AWS access used: `venice-automation` (SSM `describe-parameters` /
`get-parameter`, Lambda `get-function-configuration`, CloudWatch `filter-log-events`). Private key
material was read **in process only**; no secret value appears in this document, in any file this
work wrote, or in any log. Everything below is evidence-backed; the few unverified points are
marked **UNVERIFIED**.

---

## 0. Verdicts

| # | Finding | Verdict |
|---|---|---|
| 1 | Production clients stop firing **899–900 s (≤ 15:00) after activation**, every time, and no heartbeat can repair it. | CONFIRMED in code + live logs |
| 2 | The private half of the 07-17 lease keypair (`nKx5xim…`) **does not exist on this machine or in SSM**. | CONFIRMED (see §2 for the search) |
| 3 | Fix = **new lease keypair** + one-line `LeaseGate.cpp` change + a Lambda zip that actually contains `cryptography`. Safe because **no production build has shipped**. | RECOMMENDED |
| 4 | Update-manifest signing is **not broken**. `/orion/manifest_signing_key` is an orphan parameter no code reads; the real key chain (VeniceSigning PEM ↔ `/orion/ed25519_private_key` ↔ `/orion/ed25519_public_key` ↔ the client's pinned constant ↔ the signed `release/update_manifest.json`) **matches end to end**. | B5's key item is a FALSE ALARM |
| 5 | The **update-signing private key is exposed twice locally**: an unencrypted second copy of the PEM, and a cleartext copy inside a 2026-09-14 agent transcript. | NEW FINDING — owner decision (§3.4) |
| 6 | The Lambda holds no signing key today (CRIT-2 made manifest signing offline), but SSM still stores the update private key the Lambda's role can probably read. | RISK — recommend deleting it after an off-box backup (§3.3) |

---

## 1. The lease chain, verified end to end (deliverable A)

**Server (mint).** `backend/lambda_function.py`
- `LEASE_SIGNING_KEY_SSM = "/orion/lease_signing_key"` (L147), `LEASE_TTL_S = 900` (L1654).
- `handle_validate` (`POST /api/license/check`, L1694): after the licence checks it computes
  `lease_expires_at = now + LEASE_TTL_S` (L1776) and then
  ```python
  try:
      resp_body["lease_sig"] = sign_lease(license_key, machine_id, lease_expires_at)   # L1800
      resp_body["public_key_id"] = LEASE_KEY_ID
  except Exception as e:
      print(f"[LEASE] signing unavailable (fail-soft): {e}")                            # L1803
  ```
- `sign_lease` (L236) signs `f"{key.strip().upper()}:{machine_id.strip()}:{int(expires)}"` with the
  PEM from SSM, base64url, no padding. It needs **both** `cryptography` (absent from the live zip)
  **and** `/orion/lease_signing_key` (absent from SSM) — so today it always raises and the response
  carries **no `lease_sig`**.

**Live evidence.** `aws logs filter-log-events --log-group-name /aws/lambda/orion-activate
--filter-pattern '"[LEASE]"'` → the last four heartbeats ever served,
`2026-08-07T06:50:01Z` … `2026-08-08T05:41:10Z`, all
`[LEASE] signing unavailable (fail-soft): No module named 'cryptography'`.
`aws ssm describe-parameters` over `/orion/` returns 22 parameters; `/orion/lease_signing_key` is
not among them, and `get-parameter` on it returns `ParameterNotFound`.
Live function today: `python3.12`, x86_64, `CodeSha256 Pjk+8xr6AQ8KzgpeL/cQUZk2wC4FfMFZszyAhOJoD3Q=`,
`LastModified 2026-09-17T04:25:08Z`, `CodeSize 52564` (i.e. still `lambda_function.py` only),
no layers, `RevisionId 6168c5cc-4732-4563-b1cb-c6090ae1e99b`.

**Client (consume).**
- `LeaseGate::enabledFromEnvironment()` — `native_orion/src/LeaseGate.cpp:33-41`: under
  `ORION_PRODUCTION_BUILD` it `return true;` unconditionally. No env var can turn the gate off in a
  shipping build.
- Activation: `OrionAppController.cpp:2290` → `LeaseGate::recordActivation(tokenExpires)` →
  `LeaseGate.cpp:46-58` sets `leaseValidUntilEpochS_ = nowEpochS + kFallbackLeaseTtlS` with
  `kFallbackLeaseTtlS = 900` (`LeaseGate.h:38`). The 30-day activation token is explicitly **not**
  allowed to extend it.
- Heartbeat: `licenseHeartbeatTimer_` fires every `5 * 60 * 1000` ms (`OrionAppController.cpp:2400-2405`)
  → `LicenseClient::validate(authLicenseKey_, security_.machineId())`.
  `parseLicenseCheckResponse` (`LicenseClient.cpp:126-127`) reads `lease_expires_at` and `lease_sig`.
- The refresh gate: `OrionAppController.cpp:2360-2367`
  ```cpp
  const QByteArray leaseKey = LeaseGate::leaseVerifyPublicKey();
  if (!leaseKey.isEmpty()
      && !LeaseGate::verifyLeaseSignature(leaseKey, authLicenseKey_, security_.machineId(),
                                          result.leaseExpiresAtEpochS, result.leaseSig)) {
      appendLog("License heartbeat: lease signature INVALID — lease not refreshed.");
      return;                        // ← no recordHeartbeatOk()
  }
  ```
  `leaseVerifyPublicKey()` has been non-empty since 07-17 (`LeaseGate.cpp:165`), and
  `verifyLeaseSignature` returns `false` for an empty signature (`LeaseGate.cpp:141`). So **every**
  heartbeat from the current live backend takes the `return` branch.
- Expiry: `fireAllowed()` (`LeaseGate.cpp:84-96`) requires *both* `nowMonotonicMs - lastRefresh <=
  maxStalenessMs_` (15 min, `LeaseGate.h:33`) *and* `nowEpochS < leaseValidUntilEpochS_`.

**Time to failure — precise.** Let `E` be the integer epoch second at which the activation response
is processed. `leaseValidUntilEpochS_ = E + 900`. `fireAllowed()` returns false as soon as the
wall clock reads `E + 900`, i.e. **899.0 – 900.0 s (14 min 59 s – 15 min 00 s) after activation**,
never later: the monotonic staleness bound trips at exactly 900.000 s as well, because no heartbeat
ever refreshes `lastRefreshMonotonicMs_`. The 5-minute heartbeats at t≈300/600/900 s only add
`License heartbeat: lease signature INVALID — lease not refreshed.` to the log.

**What the user sees at t≈15 min** (all gated on `automationSecurityAllowed()`, which ANDs
`leaseGate_.fireAllowed()` via `AutomationAccessPolicy::allowed`, `OrionAppController.cpp:10935-10955`):
- the per-poll automation call is skipped and the schedule is torn down —
  `OrionAppController.cpp:13001-13010` (`disarmPreciseFire(); automation_.reset();`): the pad passes
  through, the bot simply never fires again;
- the meter gate stops arming — throttled log
  `Meter-gate arm SUPPRESSED (remote=1 embedded=0 preview=1 security=0)`
  (`OrionAppController.cpp:12745-12780`);
- reconnecting is refused with `Security lock active: Lease expired (server lease required)`
  (`OrionAppController.cpp:7497-7500`).
Restarting the launcher and unlocking again buys exactly one more 15-minute window.

---

## 2. Lease key decision (deliverable B)

### 2.1 The hunt for the 07-17 private key — negative

`LeaseGate.cpp:156-166` pins `nKx5ximQYHX1/eMB9m7tzcPtO/gtSBIaCDd4BiT4itw=`.
`docs/SERVER_DEPLOY_CHECKLIST.md` §2 shows the keypair was meant to be generated by the operator on
their own machine, the PEM put into SSM, and only the public half handed to the client.

Searched (every candidate PEM / raw-32-byte / 64-hex-char token was turned into an Ed25519 public
key **in process** and compared against both pinned keys; only paths and match/no-match are reported):

| Where | Coverage | Result |
|---|---|---|
| git history | `git log --all -S "lease_signing"`, `-S "nKx5xim"` | only the 09-14 baseline commit (history was lost 08-09) |
| repo working tree | `backend/`, `docs/`, `tools/`, `scripts/`, `native_orion/src`+`tests`, `tests/`, `release/`, `installer/`, `discord_launch/`, `.codex_artifacts/`, `.audit_raw/` — 12 891 files, 106 PEM blocks, 87 Ed25519 keys, 5 570 raw-seed candidates | **no match** |
| `D:\NexusVision\` | 41 818 files, 265 PEM blocks, 230 Ed25519 keys, 3 902 raw-seed candidates | **no match** |
| `C:\Users\aaron\Desktop\VeniceSigning\` | 1 PEM | matches the **update** key, not the lease key |
| `codesigning\` | only `artifact-signing-metadata.json` | no key present |
| `C:\Users\aaron\Desktop`, `Documents`, `Downloads`, `.aws` | PEM sweep, 358 907 files, 3 679 PEM blocks, 149 Ed25519 private keys parsed | **no match** — only the two update-key copies (§3.4) |
| AWS SSM | all 22 `/orion/*` parameters listed; `ed25519_private_key`, `manifest_signing_key`, `update_secret`, `shard_encryption_key` decrypted in process and key-derived | `/orion/lease_signing_key` **does not exist**; nothing else derives to `nKx5xim…` |
| agent transcripts (`~/.claude/projects`, `~/.codex/sessions`, 8.8 GB) | `BEGIN … PRIVATE KEY` sweep → 2 files, both parsed | only the **update** key (§3.4) + a Cloudflare docs example |

**Verdict: the lease private key is gone (or was never generated).** The client has been pinned to a
public key whose private half the server never had — which is exactly why `/orion/lease_signing_key`
was never created.

### 2.2 Consequence of a new keypair — acceptable, pre-launch

Any binary compiled with the old constant cannot verify leases signed by a new key. That is free
today because **no production build has been shipped**:
- live `GET /api/update` → `404 {"ok": false, "error": "no manifest published"}`
  (`.codex_artifacts/pairing-deploy-20260916/VERIFICATION.txt:43`), so no client can even discover a release;
- `#downloads` is empty (`docs/LAUNCH_LIVE_AUDIT_2026-09-19.md` B5);
- `release/update_manifest.json` is local only, `artifact_url: ""`, `published_at 2026-09-19T21:17:20Z`;
- `installer/Output/VeniceSetup-1.0.0.exe` is dated 2026-08-08 and superseded; the 09-19 package is
  explicitly **PROVISIONAL** (`docs/LAUNCH_PACKING_CHECKLIST.md` §0);
- the owner confirmed on 09-16 that "nobody has an older launcher and only the owner has a test build"
  (`.codex_artifacts/pairing-deploy-20260916/VERIFICATION.txt:40`).

### 2.3 The exact client change (NOT applied — the coordinator applies it with the real key)

`tools/signing/new_lease_keypair.py` (added by this work) prints the new public key; paste it here:

```diff
--- a/native_orion/src/LeaseGate.cpp
+++ b/native_orion/src/LeaseGate.cpp
@@ -153,13 +153,14 @@ QString LeaseGate::stateText() const
 QByteArray LeaseGate::leaseVerifyPublicKey()
 {
-    // NEW-1 (2026-07-17): the server (backend/lambda_function.py sign_lease) signs the
+    // NEW-1 (2026-09-19 rotation): the server (backend/lambda_function.py sign_lease) signs the
     // heartbeat lease with Ed25519 over "<license_key>:<machine_id>:<expires>". This is the
-    // matching raw 32-byte PUBLIC key (base64). Enforcement is still gated by
-    // ORION_LEASE_GATED_FIRE (default OFF) — turning it on requires the server to be DEPLOYED
-    // with lease-signing first (see docs/SERVER_DEPLOY_CHECKLIST.md), else valid leases would be
-    // rejected. To rotate: regenerate the keypair, set the private half in SSM
-    // /orion/lease_signing_key, and replace the base64 below.
-    return QByteArray::fromBase64("nKx5ximQYHX1/eMB9m7tzcPtO/gtSBIaCDd4BiT4itw=");
+    // matching raw 32-byte PUBLIC key (base64). The 07-17 key was replaced because its private
+    // half was never provisioned in SSM and could not be found (docs/SIGNING_FIX_2026-09-19.md).
+    // PRODUCTION FORCES THE GATE ON (enabledFromEnvironment above), so this constant and SSM
+    // /orion/lease_signing_key must be two halves of the SAME keypair or every customer stops
+    // firing 15 minutes after unlock. To rotate: tools/signing/new_lease_keypair.py, upload the
+    // private half, replace the base64 below, rebuild, then re-run the >20 min lease smoke.
+    return QByteArray::fromBase64("<NEW_PUB_B64 printed by new_lease_keypair.py>");
 }
```

Nothing else in the tree hard-codes the lease public key (`grep -rn nKx5xim` → `LeaseGate.cpp` and
docs only), so this one line is the whole client change.

### 2.4 Test updates (already applied, green)

- `native_orion/tests/AutomationEngineTests.cpp` — new
  `leaseGateVerifiesPythonSignedLeaseVector()`: cross-language proof, §5.
- same file, `leaseGateSignatureRoundTripAndRejection()` — added
  `QCOMPARE(LeaseGate::leaseVerifyPublicKey().size(), 32);` so a truncated or whitespace-damaged
  base64 paste during the rotation fails the build's tests instead of every customer's heartbeat.
- `tests/backend/test_new1_lease_sig.py` — new `TestCrossLanguageLeaseVector` (4 cases) pinning the
  server's signature bytes, its base64url alphabet and its trim/upper-case normalisation to the same
  vector the C++ test verifies.

---

## 3. Update-manifest signing (deliverable C)

### 3.1 How it actually works (end to end)

1. **Offline signer.** `tools/package_orion_release.py`: `--signing-key <PEM>` (or
   `ORION_UPDATE_SIGNING_KEY_PEM`), `sign_update_manifest()` (L920) signs the canonical bytes of
   `MANIFEST_SIGN_FIELDS` (L81) — `json.dumps(sort_keys=False, separators=(",",":"),
   ensure_ascii=True)` — base64url, no padding. `require_trusted_production_signing_key()` (L858)
   **refuses to package** if the PEM's public half is not the embedded
   `ED25519_PUBLIC_KEY_B64` (L84). The same key also signs `release_manifest.sig`.
2. **Publish.** `POST /api/update` (`backend/lambda_function.py:909`) is admin-gated, rate-limited,
   requires a `signature` in the body, checks `artifact_url` against `ARTIFACT_URL_ALLOWLIST`, and
   **verifies** the signature with the PUBLIC key from SSM `/orion/ed25519_public_key`
   (`verify_manifest_signature`, L211). CRIT-2 removed server-side signing: the private-key loader
   `load_ed25519_private_key` (L179) is no longer reachable from any handler.
3. **Serve.** `GET /api/update` (L853) returns the stored manifest plus `public_key_b64`.
4. **Client.** `OrionUpdater.exe` pins `ORION_UPDATE_ED25519_PUBKEY` at build time
   (`native_orion/CMakeLists.txt:359`), `ReleaseManifestTrust.h:12` carries the same constant for
   `release_manifest.sig`, and `UpdateManifest.cpp` rebuilds the identical canonical bytes
   (`docs/UPDATER_CLIENT.md` L100-125). It fails closed if the key or libcrypto is unavailable.

### 3.2 Key identities — all derived in process, all matching

| Holder | Derived public key | Matches the pin? |
|---|---|---|
| `C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem` (EFS `E`) | `OJQ2E7ZA…` | **yes** |
| SSM `/orion/ed25519_private_key` (SecureString, 06-09 07:42) | `OJQ2E7ZA…` | **yes** |
| SSM `/orion/ed25519_public_key` (String, 06-09 07:42) | `OJQ2E7ZA…` | **yes** |
| `native_orion/CMakeLists.txt:359`, `ReleaseManifestTrust.h:12`, `tools/package_orion_release.py:84`, `native_orion/tests/OrionUpdaterTests.cpp:301`, `release/update_manifest.json` | `OJQ2E7ZA…` | — (these *are* the pin) |
| `release/update_manifest.json` signature, re-verified against the pin | valid | **yes** |

**`/orion/manifest_signing_key` is an orphan.** It exists (SecureString, created 2026-06-09
**07:19:20**, 23 minutes *before* the Ed25519 pair at 07:42), its value is **64 hex characters =
32 raw bytes, not a PEM**, and interpreting those bytes as an Ed25519 seed derives a public key that
matches **neither** pin. No file in the repo reads that parameter name — `grep -rn
manifest_signing_key` hits only the two 09-19 audit documents and the 09-17 Codex verification note.
It is a leftover from the pre-CRIT-2 HMAC design (the same 64-hex shape as `/orion/update_secret` and
`/orion/shard_encryption_key`).

**Therefore B5's key item is a false alarm**: the 09-17 gate compared the wrong parameter. The real
cause of that gate failure was the missing local trusted PEM — `scripts/fetch_signing_key.ps1` writes
to `codesigning\venice_update_signing.pem`, and `codesigning\` currently holds only
`artifact-signing-metadata.json`. The fix is to point the packer at
`C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem` (or re-run
`scripts\fetch_signing_key.ps1`), not to touch any SSM key.

**Safest correct fix for §3:** change nothing about the update keys for launch. Optionally (owner's
call, after an off-box backup) delete the orphan `/orion/manifest_signing_key`, and see §3.3–§3.4.

### 3.3 Design risk: a server that can sign updates

The architecture **already** does the right thing — signing is offline and the Lambda holds only the
public key, so a Lambda compromise cannot mint a signed update. Two caveats:

- SSM `/orion/ed25519_private_key` still holds the update-signing private key. If the
  `orion-activate-lambda-role` SSM policy is written as `/orion/*` (**UNVERIFIED** — `venice-automation`
  cannot read IAM), then code execution in the Lambda *can* read the update key even though the
  handler never asks for it, which re-creates the blast radius CRIT-2 removed.
  `docs/SERVER_DEPLOY_CHECKLIST.md:14` already says the parameter should be offline-only and may be
  deleted. **Recommendation:** owner makes an off-box backup of the PEM, then deletes
  `/orion/ed25519_private_key`, or scopes the role's SSM `Resource` list to the exact parameters the
  code reads. Note `scripts/fetch_signing_key.ps1` is the only tooling that uses it; after deletion
  the PEM path must be passed explicitly.
- By contrast the **lease** key has to live server-side (the server mints a lease per heartbeat), and
  that is fine: its blast radius is "forge fire leases for licences you already know", bounded by the
  revoke-fast kill codes and fixable by one key rotation + client rebuild. Keep it a **separate** key
  from the update key (as the code already does) so a Lambda compromise can never sign a binary.
- Stale prose worth fixing when someone is next in the file: `tools/package_orion_release.py:1129`
  still says "server signs with the SSM Ed25519 key" — untrue since CRIT-2.

### 3.4 NEW: the update-signing private key is exposed locally (owner decision)

1. `C:\Users\aaron\Desktop\EXPLOITS\helios_bypass\codesigning\venice_update_signing.pem` is a second,
   **unencrypted** (`cipher /c` → `U`) copy of the production update-signing key. The packing
   checklist believes the EFS copy is the only one. Nothing was moved or deleted.
2. The same key appears in **cleartext inside an agent transcript**:
   `C:\Users\aaron\.claude\projects\C--Users-aaron-Desktop-NexusVision\7396b864-…\subagents\agent-a801f0de6b5a1390d.jsonl`
   (record dated 2026-09-14T06:59:00Z) — a tool result that printed `--- codesigning pem head ---`
   followed by the whole 3-line PEM. Anything that read that transcript has the private key.

**Recommendation (owner):** rotate the update keypair before the public release. It is cheap *now*
and expensive later — the same key signs `release_manifest.sig`, so after launch a rotation means a
re-pack, a re-signed installer and a forced update. Rotation = generate a new PEM offline, update
`ED25519_PUBLIC_KEY_B64` (`tools/package_orion_release.py:84`), `ORION_UPDATE_ED25519_PUBKEY`
(`native_orion/CMakeLists.txt:359`), `kReleaseManifestPublicKeyB64` (`ReleaseManifestTrust.h:11-12`),
`OrionUpdaterTests.cpp:301`, SSM `/orion/ed25519_public_key`, then re-run the strict gate and re-sign
the package. Do it in the **same** pass as the lease-key change so the client is rebuilt once.
If the owner declines: delete the unencrypted copy, keep the EFS one, and treat the transcript as
sensitive.

---

## 4. Lambda deploy package (deliverable D)

`backend/build_lambda_zip.ps1` (added by this work) builds the package and **uploads nothing**.

```powershell
powershell -ExecutionPolicy Bypass -File backend\build_lambda_zip.ps1
```

What it does: `pip install --platform manylinux2014_x86_64 --implementation cp --python-version 3.12
--only-binary=:all: --no-compile --target <staging> cryptography==50.0.1`, copies the current
`backend/lambda_function.py`, verifies, then writes a **deterministic** zip (sorted entries, fixed
1980 timestamps, Unix `0644` modes, `create_system=3`). It never uses `Compress-Archive`, which on
Windows PowerShell writes backslash entry names that Lambda cannot unpack.

Pre-zip refusals (any one fails the build and no zip is written): a non-ELF / non-x86-64 / >glibc-2.34
native module, any Windows `MZ` binary (this is why pip's `bin\cffi-gen-src.exe` launcher is dropped),
a wheel tag that is not manylinux-x86_64 or pure-python or not cp312-compatible, a vendored
runtime-provided package (`boto3`/`botocore`/…), any `*.pem|key|pfx|p12|crt`, a `PRIVATE KEY` block in
the handler, or a zip over the 50 MiB direct-upload limit.

**Imports audited (AST, whole file):** stdlib `base64, datetime, decimal, hashlib, hmac, ipaddress,
json, os, secrets, string, time, urllib, uuid`; third-party **`boto3`** (runtime-provided — deliberately
NOT vendored) and **`cryptography`** (L182, 213, 214, 231, 2756, 2763). No dynamic
`__import__`/`importlib`. So `cryptography` + its `cffi`/`pycparser` chain is the complete gap.

**Build result (2026-09-19):**

| | |
|---|---|
| zip | `D:\NexusVision\signing\orion-activate.zip` |
| size | **5 035 388 bytes (4.80 MiB)** |
| sha256 | `72f8df8785e22ca6b27cb4242c0173d97ef1d887ae47a4cc39a3397a741d3833` |
| CodeSha256 (base64, what `get-function` will show) | `cvjfh4XiLKayfLQkLAFz2X7x2IeuR6TMOaM5enQdODM=` |
| entries | 169 (full list: `D:\NexusVision\signing\orion-activate.zip.files.txt`) |
| top level | `lambda_function.py`, `cryptography/`, `cryptography-50.0.1.dist-info/`, `cffi/`, `cffi-2.1.1.dist-info/`, `pycparser/`, `pycparser-3.0.dist-info/`, `_cffi_backend.cpython-312-x86_64-linux-gnu.so` |
| handler sha256 | `82b9b6f6bcd829b009760a95c09e22ef681234f8addac5426a137d8292021893` (`backend/lambda_function.py`, 4 916 lines, includes the other agent's website-guild routes) |

**Package proof** (no Linux runtime on this box — no WSL distro, no Docker — so this is
tag+binary-level, not an executed import):
- `cryptography-50.0.1` wheel tags `cp311-abi3-manylinux_2_17_x86_64, cp311-abi3-manylinux2014_x86_64`;
  `cffi-2.1.1` tags `cp312-cp312-manylinux_2_17_x86_64, …manylinux2014_x86_64`; `pycparser-3.0` is
  `py3-none-any`. cp311-abi3 is loadable by cp312; manylinux_2_17 ≪ AL2023's glibc 2.34.
- `cryptography/hazmat/bindings/_rust.abi3.so`: ELF64, `e_machine 0x3E` (x86-64), max referenced
  symbol version **GLIBC 2.17**. `_cffi_backend…so`: ELF64, x86-64, max **GLIBC 2.14**.
- No `.pyd`/`.dll`/`MZ` file and no `__pycache__` in the zip; nothing from `boto3`/`botocore`.
- The signing code path itself is proven by the cross-language test in §5 (local `cryptography`
  50.0.0, same `sign_lease` source) — the vendored Linux build of the same library version is what
  the verification step in §6 confirms at runtime.

---

## 5. Cross-language proof (deliverable E)

A lease signed by the **Python** `sign_lease` path is verified by the **C++**
`LeaseGate::verifyLeaseSignature` for the same key. Both sides pin one vector, produced with the
deterministic **TEST** key from `tests/backend/conftest.py` (seed
`b"orion-test-lease-signing-key-032"`) — never the production key.

- Python (`tests/backend/test_new1_lease_sig.py::TestCrossLanguageLeaseVector`): calls the real
  `lambda_function.sign_lease` under moto and asserts the signature is exactly the pinned base64url
  string, that it contains no `=`/`+`/`/`, that the test public key is the pinned one, and that
  leading/trailing whitespace + lower-case input normalise to the same signature (the client trims and
  upper-cases before verifying).
- C++ (`native_orion/tests/AutomationEngineTests.cpp::leaseGateVerifiesPythonSignedLeaseVector`):
  derives the public key from the same seed and compares it to the pinned base64; verifies the
  Python-made signature; **re-signs** the same lease and asserts byte equality with it (proving the
  canonical message matches Python's f-string exactly); verifies again with a lower-case licence key;
  and rejects a bumped expiry, a different machine id, a different key, and standard-base64 in place
  of base64url.

Results:

| Suite | Command | Result |
|---|---|---|
| backend | `cd tests/backend && ../../.venv/Scripts/python.exe -m pytest -q --basetemp=D:/NexusVision/pytest_tmp/signing` | **324 passed** (was 320; +4 new) |
| lease only | `… -q test_new1_lease_sig.py` | **7 passed** (3 existing + 4 new) |
| native | throwaway build `C:/Users/aaron/obld_s`, target `OrionNativeTests` (Release), then the 11 lease/licence test functions | **13 passed, 0 failed, 0 skipped** (incl. `initTestCase`/`cleanupTestCase`; log `D:\NexusVision\signing\leasegate_tests2.txt`) |

The native build used `-DORION_LIBCRYPTO_PATH=…/native_orion/build/Release/libcrypto-3-x64.dll` so
`ed25519Available()` is true and the new test really runs instead of `QSKIP`-ing. `native_orion/build`
and `native_orion/build_prod_codex` were not touched; delete `C:\Users\aaron\obld_s` when done.

---

## 6. EXECUTE — exact commands for the coordinator

Run in this order. Steps 1–4 are the launch fix; step 5 proves it; step 6 is the manifest cleanup.
`<NEW_PUB_B64>` is printed by step 1 and is **not** a secret.

### Step 0 — preflight (read-only)

```powershell
cd C:\Users\aaron\Desktop\NexusVision
aws sts get-caller-identity
aws lambda get-function-configuration --function-name orion-activate --region us-east-1 `
  --query "{Sha:CodeSha256,Rev:RevisionId,Mod:LastModified}"
# expect Sha=Pjk+8xr6AQ8KzgpeL/cQUZk2wC4FfMFZszyAhOJoD3Q= ; if it differs, someone deployed since
# 2026-09-19 — re-read the live code before continuing.
```

### Step 1 — generate the lease keypair and put the private half in SSM

```powershell
.\.venv\Scripts\python.exe tools\signing\new_lease_keypair.py --out-dir D:\NexusVision\signing
# prints ONLY the public key + the commands below; the PEM is written to
# D:\NexusVision\signing\lease_signing_key.pem, ACL-restricted to the current user.

aws ssm put-parameter --name /orion/lease_signing_key --type SecureString `
  --value file://D:/NexusVision/signing/lease_signing_key.pem --region us-east-1
# (KMS: default alias/aws/ssm, same as every other /orion/* SecureString. Add --overwrite ONLY if
#  the parameter already exists — today it does not.)

# confirm SSM holds the half that matches what you are about to pin:
.\.venv\Scripts\python.exe tools\signing\new_lease_keypair.py --verify-ssm
# then destroy the local copy:
.\.venv\Scripts\python.exe tools\signing\new_lease_keypair.py --shred D:\NexusVision\signing\lease_signing_key.pem
```

**Do not** paste the PEM into a shell argument, a chat message, a commit, or any file under the repo.

### Step 2 — pin the public half in the client

Apply the §2.3 diff to `native_orion/src/LeaseGate.cpp` with `<NEW_PUB_B64>`, then:

```powershell
# fast check that the constant is well-formed before a full rebuild
.\.venv\Scripts\python.exe -c "import base64,sys; b=base64.b64decode('<NEW_PUB_B64>'); print('bytes',len(b)); sys.exit(0 if len(b)==32 else 1)"
```

Hand the change to the packing agent: the production build, `release_manifest.sig` and
`update_manifest.json` must **all** be regenerated afterwards
(`docs/LAUNCH_PACKING_CHECKLIST.md` §0 — the current 09-19 package is provisional).

### Step 3 — build the Lambda zip (after the backend edits are final)

```powershell
powershell -ExecutionPolicy Bypass -File backend\build_lambda_zip.ps1
# Verify the handler hash printed at the end matches the file you intend to ship:
Get-FileHash backend\lambda_function.py -Algorithm SHA256
# Current expected: 82b9b6f6bcd829b009760a95c09e22ef681234f8addac5426a137d8292021893
# (rebuild the zip if backend\lambda_function.py changes again — the zip embeds a copy.)
```

### Step 4 — deploy the Lambda (needs the owner's OK; this is the only live change here)

This deploy is **not crypto-only**: the same file carries the backend agent's new website routes
(`/api/bot/guild-join`, `/api/bot/guild-member`, trial throttle, billing-portal DM copy). Those
routes stay unreachable until the owner adds them to the API Gateway (see below), but the rest of
the diff goes live with this command.

```powershell
# keep the current bytes for rollback (the URL is a short-lived presigned link)
$loc = aws lambda get-function --function-name orion-activate --region us-east-1 `
         --query "Code.Location" --output text
Invoke-WebRequest -Uri $loc -OutFile D:\NexusVision\signing\orion-activate-ROLLBACK.zip
# (D:\NexusVision\live_audit\live_orion-activate.zip is the same bytes, captured 09-19)

aws lambda update-function-code --function-name orion-activate --region us-east-1 `
  --zip-file fileb://D:/NexusVision/signing/orion-activate.zip `
  --revision-id 6168c5cc-4732-4563-b1cb-c6090ae1e99b     # refresh from Step 0 if it changed

aws lambda get-function-configuration --function-name orion-activate --region us-east-1 `
  --query "{Sha:CodeSha256,Status:LastUpdateStatus,Size:CodeSize}"
# expect Sha=cvjfh4XiLKayfLQkLAFz2X7x2IeuR6TMOaM5enQdODM=, Status=Successful, Size≈5.0 MB
```

Rollback: `aws lambda update-function-code … --zip-file fileb://<the saved zip>`.

### Step 5 — SAFE live verification (mints nothing)

**5a. Log watch (zero requests).** Ask the owner to unlock their own launcher once, then:

```powershell
$since = [DateTimeOffset]::UtcNow.AddMinutes(-20).ToUnixTimeMilliseconds()
aws logs filter-log-events --log-group-name /aws/lambda/orion-activate --region us-east-1 `
  --filter-pattern '"[LEASE]"' --start-time $since --query "events[].message" --output text
```
- **PASS = no output at all** (nothing to fail-soft about).
- `No module named 'cryptography'` → the zip did not land; redo Step 3/4.
- `ParameterNotFound` → Step 1's `put-parameter` did not take.
- `AccessDeniedException … ssm:GetParameter … /orion/lease_signing_key` → the
  `orion-activate-lambda-role` policy does not cover the new parameter name. **Owner (IAM)** must add
  `ssm:GetParameter` on `arn:aws:ssm:us-east-1:987622176566:parameter/orion/lease_signing_key`.
  This is the single most likely residual failure — the role's policy could not be read from here.

**5b. One heartbeat for a licence that already exists** (stamps `last_check_at` only — no key is
minted, no entitlement changes). The owner supplies their own `license_key` + `machine_id`:

```powershell
$edge = aws --% ssm get-parameter --name /orion/edge_auth_secret --with-decryption --query Parameter.Value --output text
$body = @{ license_key = "<OWNER-KEY>"; machine_id = "<OWNER-MACHINE-ID>" } | ConvertTo-Json -Compress
$r = Invoke-RestMethod -Method POST -Uri "https://api.zaeorion.com/api/license/check" `
       -Headers @{ "X-Edge-Auth" = $edge; "Content-Type" = "application/json" } -Body $body
$r.lease_sig; $r.public_key_id; $r.lease_expires_at
# PASS = lease_sig is present and public_key_id = orion-lease-ed25519-v1.
```

Then verify that signature against the pinned key (public data only):

```powershell
.\.venv\Scripts\python.exe -c "import base64,sys;from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey;k,m,e,s='<OWNER-KEY>','<OWNER-MACHINE-ID>',<EXPIRES>,'<SIG>';p=Ed25519PublicKey.from_public_bytes(base64.b64decode('<NEW_PUB_B64>'));p.verify(base64.urlsafe_b64decode(s+'='*(-len(s)%4)), f'{k.strip().upper()}:{m.strip()}:{int(e)}'.encode());print('lease signature VALID under the pinned client key')"
```

**5c. The real gate (blocking for release).** `docs/LAUNCH_PACKING_CHECKLIST.md` Step 6: the
production build must keep firing for **> 20 minutes** after unlock. That is the only proof the
whole chain works; it must run on the build made after Step 2.

### Step 6 — manifest keys (no code change)

```powershell
# a) point the packer at the trusted PEM (or re-fetch it) — this is what actually failed on 09-17:
$env:ORION_UPDATE_SIGNING_KEY_PEM = "C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem"
#   or:  powershell -ExecutionPolicy Bypass -File scripts\fetch_signing_key.ps1

# b) OPTIONAL, owner decides: retire the orphan parameter no code reads (§3.2).
#    Back up the value first if anything outside this repo might use it.
# aws ssm delete-parameter --name /orion/manifest_signing_key --region us-east-1

# c) OPTIONAL, owner decides: after an OFF-BOX backup of venice_update_signing.pem,
#    remove the update private key from SSM so a Lambda compromise cannot read it (§3.3).
# aws ssm delete-parameter --name /orion/ed25519_private_key --region us-east-1

# d) owner decision on §3.4 (rotate the update keypair, or delete the unencrypted second copy).
```

### Also required before the website's new routes work (from the backend agent)

The HTTP API `v348t5hg3i` uses **explicit** routes — unknown paths return `{"message":"Not Found"}`
at the gateway. `POST /api/bot/guild-join` and `POST /api/bot/guild-member` must be added to the API
and attached to the existing `orion-activate` integration **before** the Worker deploy.
`venice-automation` has no `apigateway:*` permission (`GetRoutes` → AccessDenied), so this is an
**owner** step in the console.

### Who can do what

| Step | `venice-automation` | Owner |
|---|---|---|
| 1 SSM `PutParameter` `/orion/lease_signing_key` | **yes** — a Codex agent created `/orion/worker_bot_secret` with these credentials on 09-16 (policy not readable, so treat as evidenced, not proven) | fallback if denied |
| 2 `LeaseGate.cpp` + rebuild | coordinator / packing agent | approves the key constant |
| 3 build the zip | **yes** (local only) | — |
| 4 `lambda:UpdateFunctionCode` | **yes** — used for the 09-16 deploy, dry-run + real | **approval required** (live change) |
| 5a CloudWatch read | **yes** | runs the launcher |
| 5b heartbeat probe | **yes** (needs the edge secret, read in process) | supplies key + machine id |
| 5c > 20-min lease smoke | — | **owner only** (real console + build) |
| 6b/6c SSM `DeleteParameter` | probably (**UNVERIFIED**) | **owner decision** |
| IAM: grant the role `ssm:GetParameter` on the new parameter, if 5a says AccessDenied | no (IAM denied) | **owner only** |
| API Gateway routes for the two new bot paths | no (apigateway denied) | **owner only** |

---

## 7. Open / unverified

- The `orion-activate-lambda-role` SSM policy could not be read (`iam:*` denied), so whether the role
  can read the **new** `/orion/lease_signing_key` is unknown until Step 5a. This is the most likely
  residual failure and its fix is owner-only.
- `venice-automation`'s exact permissions are inferred from what previous agent runs did (SSM
  PutParameter on 09-16, Lambda UpdateFunctionCode on 09-16), not from a policy read.
- The vendored package was not executed on Linux (no WSL distro, no Docker on this box); §4's proof is
  wheel-tag + ELF/glibc level, and Step 5a is the runtime confirmation.
- The key search covered files ≤ 4 MiB, skipped media/model/archive extensions and the `framedump*`
  trees, and cannot see anything the owner keeps off this machine (password manager, phone, paper).
  If the owner *does* hold the 07-17 lease PEM, uploading it to `/orion/lease_signing_key` is a valid
  alternative to Steps 1–2 and needs no client rebuild — confirm with
  `new_lease_keypair.py --verify-ssm`, which must print `nKx5ximQYHX1/eMB9m7tzcPtO/gtSBIaCDd4BiT4itw=`.
