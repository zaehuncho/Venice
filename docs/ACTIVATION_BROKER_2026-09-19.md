# Venice activation broker — build spec (server-shard blocker #5)

**Why this exists.** Server-shard makes the shipped `OrionNative.exe` incomplete on disk: it
decrypts only after `POST /api/shard/retrieve` returns its 32-byte fragment. The Lethe
`shard_bootstrap` presents a **customer session** (`token`, `token_id`, `machine_id`) it reads
from a DPAPI file — but on a clean install nothing writes that file, because the only code that
runs `/api/activate` today lives *inside* the packed `OrionNative.exe`, which cannot start until
the shard is retrieved. That is the first-launch cycle. The **activation broker** breaks it: a
small, code-signed, **unpacked** helper that runs before the packed payload, performs activation,
and writes the DPAPI session the bootstrap needs. This is server-shard gap #5 and the last hard
blocker before a shard-enabled pack.

## Contracts the broker MUST match (extracted from live code, 2026-09-19)

### 1. DPAPI session file — what the broker writes, the bootstrap reads
- Path: `%LOCALAPPDATA%\NexusVision\Orion Native\.vault\shard_session.dat`
  (matches `SESSION_FILE = "NexusVision\\Orion Native\\.vault\\shard_session.dat"` in
  `Lethe/bootstrap/shard_bootstrap.c`).
- Bytes: `CryptProtectData` output (DPAPI, **CurrentUser** scope, no extra entropy — the
  bootstrap calls `CryptUnprotectData(&input, NULL, NULL, NULL, NULL, 0, ...)`).
- Plaintext before protection: JSON with exactly these string fields, read by
  `read_session_credential()` via `json_string_field`:
  - `token`     — the customer session bearer token (raw; server stores only its sha256).
  - `token_id`  — sent as the `X-Orion-Token-Id` header.
  - `machine_id`— **must satisfy `is_hex_string(machine_id, 64)`** (64 lowercase hex chars).
- The bootstrap `SecureZeroMemory`s these; the broker must likewise zero its buffers and not
  log, print, or persist the token anywhere but this DPAPI blob.

### 2. Retrieval wire (the bootstrap makes this call; the broker only enables it)
`POST https://<api>/api/shard/retrieve`, headers `Authorization: Bearer <token>` +
`X-Orion-Token-Id: <token_id>`, body `{"build_id","machine_id"}`. The Lambda
(`_customer_shard_session` + `handle_shard_retrieve`) checks: token hash (constant-time),
token expiry, `token.machine_id == body.machine_id`, then license status/revocation/expiry,
Discord entitlement, global kill, build revocation, and a per-`(license,machine)` rate limit
(`SHARD_RETRIEVE_LIMIT`). So the broker's only job is to make that session exist and bind the
right machine_id.

### 3. machine_id parity (design D3 — the correctness risk)
`handle_shard_retrieve` requires `token.machine_id == lic.machine_id == body.machine_id`, and
`lic.machine_id` is whatever the client sent to `/api/activate`. The in-app client is
`SecurityManager::machineId()` (`native_orion/src/SecurityManager.cpp:367`), which hashes:
`QSysInfo::machineHostName()` + `QSysInfo::machineUniqueId()` +
`registryValueMachineGuid()` (HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid), to a 64-hex
digest. **The broker must produce a byte-identical machine_id.** If it drifts, every clean
install fails machine binding — it fails closed (no security hole) but the app never starts.

Decision (recommended): factor the machine-id derivation into ONE shared translation unit that
both `SecurityManager` and the broker compile, and add a test asserting the broker's machine_id
equals `SecurityManager::machineId()` on the same host. Do NOT hand-reimplement it in the broker
from scratch — that is exactly the stub-vs-SecurityManager drift D3 calls out.

## Broker flow (clean install)
1. Installer registers the broker (not the packed exe) as the `orion://` protocol handler and the
   Start-menu/desktop target. Customer clicks `orion://activate?code=PAIR-…` from the website
   `/connect` (5-min TTL, single-use PAIR code — customers never receive a license key).
2. Broker parses the PAIR code, derives `machine_id` (shared code), and calls `POST /api/activate`
   with `{code, machine_id}` over TLS 1.2+ (no redirects). `handle_activate` binds the machine and
   returns the session `{token, token_id, ...}`.
3. Broker writes `{token, token_id, machine_id}` as JSON, `CryptProtectData` (CurrentUser),
   atomically to `…\.vault\shard_session.dat` (0600-equivalent ACL, dir created if absent).
4. Broker launches the packed `OrionNative.exe`. From then on every launch reads only the DPAPI
   record; no re-activation, no key on disk.
5. Failure states render the plain-English messages the Lambda returns (offline, machine mismatch,
   subscription inactive, rate limited, build revoked) — no silent fallback, no offline unlock.

## Build / sign / install (owner + coordinator)
- Language/build: plain C or minimal C++ (no Qt runtime dependency beyond the shared machine-id TU),
  WinHTTP for the TLS call, `dpapi`/`crypt32` for the blob. CMake target `OrionActivate`, x64.
- **Code signing is an owner step** (`Desktop\VeniceSigning`); the broker exe and the installer must
  both be signed. The installer registers the broker as `orion://` handler + shortcut target, and
  ships it unpacked alongside the packed payload.
- `pack_lethe_release.py --server-shard` stays fail-closed until the broker is present and has
  passed its clean-install test (gap #8, do not relax).

## Acceptance (owner rig, `scripts/accept_server_shard.ps1`)
Maps to the broker directly: (1) fresh install + Discord connect starts the packed app [broker
writes the session]; (2) network block → reachability message, no fallback; (3) revoked build →
update message; (4) machine-B copy → machine-mismatch; (5) 20-min signed-lease continuity. (1)-(5)
must PASS before ship. This spec + broker must also pass Codex security review and StrictSecurity.

## Implementation (2026-09-20, unsigned; owner steps still open)

The broker is now implemented and building on the native toolchain:

- `native_orion/src/MachineIdentity.{h,cpp}` — the ONE shared machine-id TU (design
  D3). `SecurityManager::machineId()` now delegates to `orion::deriveMachineId()`;
  the broker compiles the same source. Added to both the inline and the OLLVM
  (`native_orion/security_core/CMakeLists.txt`) SecurityCore source lists.
- `native_orion/broker/OrionActivate.cpp` — WinMain broker: registers `orion://`
  (HKCU, mirrors `main.cpp`), parses `orion://activate?code=PAIR-…`, derives
  `machine_id` via the shared TU, POSTs the activation with WinHTTP (TLS 1.2+, no
  redirects), writes the DPAPI session atomically with a current-user-only DACL,
  then launches `OrionNative.exe`. No offline fallback; fails closed with the D6
  messages; `SecureZeroMemory`s every token buffer; never logs the token.
- `native_orion/broker/BrokerContract.{h,cpp}` — pure, I/O-free request/DPAPI/
  error-mapping helpers, shared with the tests.
- Tests (CMake `#11`/`#12`, both PASS on this host):
  `OrionMachineIdParityTests` (broker's compiled `deriveMachineId()` ==
  `SecurityManager::machineId()`, 64-hex) and `OrionActivateContractTests` (real
  DPAPI protect→unprotect parsed by a VERBATIM copy of `shard_bootstrap.c`, plus
  the activate request shape and D6 error mapping).

**Wire-path note:** the broker POSTs `/api/license/redeem`, NOT `/api/activate` —
Cloudflare's free-plan managed rule 403s any path containing "activate" and the
Lambda aliases redeem→`handle_activate()`. This mirrors `LicenseClient::postActivate`;
the spec's "POST /api/activate" is the logical endpoint.

## Installer change needed (OWNER step — described, not performed)

The public installer must, for the server-shard package:
1. Ship `OrionActivate.exe` **unpacked and Authenticode-signed** alongside the
   packed payload (`OrionNative.exe` = Lethe bootstrap, `OrionNative.packed.exe` =
   packed app). Sign with `Desktop\VeniceSigning`.
2. Register **the broker** (not the packed exe) as the `orion://` protocol handler
   and as the Start-menu/desktop shortcut target. The broker also self-registers
   `orion://` on every run (idempotent) and accepts `OrionActivate.exe --register`
   as an install-time hook, but the installer should still write the shortcut.
3. **Re-registration hazard — FIXED 2026-09-20.** The packed app's
   `main.cpp:registerOrionProtocolHandler()` re-pointed `orion://` at itself on every
   launch, which would have overridden the broker after first run and broken
   re-activation (e.g. after `/hwid_reset` or on a new PC). Fixed with a runtime
   guard: `registerOrionProtocolHandler()` now returns early when
   `OrionActivate.exe` exists beside the payload (i.e. a server-shard install),
   leaving the handler to the broker. Chosen over a compile-time flag because the
   packer packs the EXISTING `OrionNative.exe` (no shard-specific rebuild), so a
   build define would be silently forgotten. Normal (non-shard) installs ship no
   broker, so behaviour there is unchanged. Full-app rebuild not run locally (heavy
   GUI build); change uses only already-included symbols (QFileInfo/QDir/
   QCoreApplication). Still a Codex-review item to confirm the guard's sufficiency.

## Status
- Server side: SSM `/orion/shard_encryption_key` present + valid (32-byte hex). Backend
  (`backend/lambda_function.py`) carries the shard handlers + lease fix, tests green, NOT deployed.
- Owner-only, still open: create DynamoDB `orion-shards`; attach the four API Gateway routes on
  `v348t5hg3i`; sign broker + installer; run rig acceptance.
- `tools/security/pack_lethe_release.py --server-shard` now ASSEMBLES + VERIFIES the
  chain fail-closed (2026-09-20 packer hardening): it Lethe-packs the app to
  `OrionNative.packed.exe`, stages the bootstrap as `OrionNative.exe` and the UNPACKED
  broker as `OrionActivate.exe`, signs the customer manifest, and
  `validate_server_shard_release_mode` VERIFIES every shard essential is present AND
  covered by that signed manifest (never merely removes the block). The owner is
  cert-free: the broker is NOT Authenticode-signed here; the Ed25519-signed release
  manifest that covers `OrionActivate.exe` is its package-INTEGRITY record (its round-3
  self-check verifies against the pinned manifest key) but is **NOT a sufficient sole bootstrap trust anchor** — a replacement broker can simply omit the self-check, and Windows loads the broker's app-local imports before its code runs (red team 2026-09-20; see the OPEN blocker in `docs/PACKING_RUNBOOK.md`). `installer/orion.iss` auto-detects the
  broker and points the launch target, shortcuts, and `orion://` registration at it
  (`RegisterActivationBroker` calls `OrionActivate.exe --register`), shipping the signed
  `release_manifest.json/.sig` beside it. Real end-to-end pack (built bootstrap/packed
  app on a pinned clean Lethe + the VeniceSigning key + clean-VM chain launch) is the
  owner's rig step.
- Related: `docs/SERVER_SHARD_2026-09-19.md`, `Lethe/bootstrap/shard_bootstrap.c`.

## 2026-09-20 hardening round 2 (Codex re-review follow-ups; unsigned, uncommitted)

Three code blockers from the Codex re-review were addressed. Nothing was deployed,
signed, packed, or committed; the packer stays unconditionally fail-closed (gap #8).

- **HIGH — manifest trust is now SIGNATURE-verified, not just hashed.** The broker
  trust logic was consolidated into ONE shared TU, `native_orion/src/BrokerInstallTrust.{h,cpp}`
  (compiled into `SecurityCore`), used by BOTH `main.cpp` (decides whether to DEFER
  the `orion://` handler to a sibling broker) and `broker/OrionActivate.cpp`
  (`runningFromVerifiedInstallRoot()` decides whether it may SELF-REGISTER). A file
  is genuine iff EITHER gate holds, with NO unauthenticated fallback:
  - (A) `fileAuthenticodeSignedByVenice()` — valid Authenticode signature whose
    signer subject contains `kExpectedBrokerSignerSubstr` (+ optional SHA-1 pin).
  - (B) `releaseManifestCoversFileSigned()` — `release_manifest.json` carries a
    detached Ed25519 signature (`release_manifest.sig`) that verifies against the
    **pinned** production key (`orion::kReleaseManifestPublicKeyId` /
    `kReleaseManifestPublicKeyB64` in `ReleaseManifestTrust.h`) over the EXACT
    manifest bytes, with the production fields enforced (schema
    `orion.release_manifest.v1`, `audience==customer`, `signature_required==true`,
    `signature_alg==ed25519`, `public_key_id==` the pinned id), AND the target file
    is covered by an exact SHA-256. This REUSES the updater's verifier
    (`orion::ed25519Verify` / `decodeEd25519PublicKey`, libcrypto-backed) — no new
    crypto was invented. Signature is MANDATORY here (unlike SecurityManager's
    dev-optional path). The old "sibling `OrionNative.exe` exists" check is GONE.
  - **OWNER STILL MUST**: set the real CN in `kExpectedBrokerSignerSubstr` and the
    leaf SHA-1 in `kExpectedBrokerSignerThumbprint` (BrokerInstallTrust.h) from
    `Desktop\VeniceSigning`; until the broker is Authenticode-signed, gate (B) (the
    signed manifest) is what authorizes.
- **MED — complete fail-closed.**
  - `BrokerContract.cpp StrictJson::parseNumber` now implements the RFC-8259 number
    grammar (rejects `-`, `01`, `1.`, `1..2`, `1e`, `1e+`, `+5`, `0x1`, ...).
  - `OrionActivate.cpp` read loop: a `WinHttpReadData`/`QueryDataAvailable` failure
    or an over-cap body now marks a TRANSPORT failure and never parses the partial
    body (factored into `finalizeActivateResponse()` so it is unit-testable).
  - `SessionStore.cpp ensureVaultDir()`: if the owner-only SD can't be built or
    `SetNamedSecurityInfoW` fails, it FAILS (no vault dir, no session write) — no
    default-DACL fallback.
  - Rollback after a failed `CreateProcess` now checks `removeSession()` and reports
    an explicit error (exit 8) if the session could not be deleted.
- **MED — zeroization finished.** Every StrictJson accumulator/intermediate is
  wiped via RAII on every path (incl. malformed-input exits), values are
  move-assigned rather than copied, and the WinHTTP `response` buffer is
  preallocated (bounded 64 KiB) so growth never orphans an unwiped token-bearing
  allocation.

New tests (all PASS locally, Release): `OrionBrokerInstallTrustTests` (manifest
matrix: unsigned / wrong-key / bad-sig / wrong-key-id / wrong-audience /
hash-mismatch / production-ignores-override all fail, only the correctly-signed
case passes — signed with an EPHEMERAL in-test keypair via the dev override, never
a real key); plus new vectors in `OrionActivateContractTests` (malformed numbers,
read-failure-is-transport-failure) and `OrionBrokerSessionStoreTests` (SD-build
failure fails closed, undeletable-session rollback reports failure). The broker now
links `SecurityCore` (pulls Qt6::Network + wintrust + libcrypto at runtime — all
already shipped beside the broker in the server-shard package).

Still needs the OWNER's RIG: sign the broker + set the signer constants; live
pinned/unpinned TLS handshake test; full StrictSecurity pass.
