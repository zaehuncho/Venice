# Venice SERVER-SHARD release design and execution gate

**Status:** implementation may proceed for the server contract and pack-time safety,
but the release remains **BLOCKED** until the first-launch identity bootstrap described
under D2 is implemented and accepted on the owner's rig. Nothing in this document is
authorization to deploy.

## Design decisions

### D1 — one shard per release build

One random 32-byte fragment belongs to one signing-stable release `build_id`.
`orion-shards` rows for public releases carry no customer `license_id`, no `hwid_hash`,
and no build-wide activation cap. Customer enforcement is performed against the live
license and its machine-bound session on every retrieval. Per-customer packed binaries
are out of launch scope.

### D2 — identity presented by the bootstrap

The shipped bootstrap must present the **customer session token**, its token id, and the
machine id established by `/api/activate`; it must not present a copied Discord id or a
long-lived license key. The token is revocable and already maps server-side to the
canonical license row.

The current executable layout has a first-launch cycle and therefore is not shippable:

1. The inherited `Lethe/bootstrap/shard_bootstrap.c` required `settings.json`
   and a plaintext `license_key` before it called `CreateProcess`. It now reads
   only the DPAPI session record, but nothing currently creates that record on
   a fresh installation.
2. The clean installer does not contain that file or key.
3. `OrionNative.exe` receives a canonical key only after its own activation callback,
   but the server-sharded `OrionNative.exe` cannot reach that callback until the
   bootstrap retrieves the fragment.
4. The entitlement cache deliberately stores only `token_present`, not the token, token
   id, machine id, or canonical key.

Consequently a fresh public install cannot satisfy acceptance step (a). The required
fix is an **outer, signed activation broker** that runs before the packed payload,
accepts the existing `orion://activate?code=PAIR-…` handoff, calls `/api/activate`, and
stores `{token, token_id, machine_id}` with Windows DPAPI in the per-user Venice data
directory. Subsequent launches read only that DPAPI record. A public installer must
register the broker, not the encrypted inner executable, as the protocol handler and
Start-menu target. No shard-enabled customer package may be produced until that broker
exists and has a clean-install test.

### D3 — one machine identity

The bootstrap reads the machine id from the DPAPI session record written by the signed
activation broker/app. It does not independently derive an HWID. This avoids the current
drift: the stub hashes hostname + SMBIOS UUID + MachineGuid, while
`SecurityManager::machineId()` hashes `QSysInfo::machineHostName()` +
`QSysInfo::machineUniqueId()` + MachineGuid. The two are not the same contract.

### D4 — retrieval authentication

`POST /api/shard/retrieve` is the sole edge-auth exemption. It requires all of:

- `Authorization: Bearer <customer session token>`
- `X-Orion-Token-Id: <token id>`
- JSON `{build_id, machine_id}`

The Lambda compares the token hash in constant time, checks token expiry and machine
binding, resolves the canonical license, and then re-checks license status, revocation,
expiry, Discord entitlement, global kill state, and build revocation. No shared edge or
admin secret is embedded in the customer executable. Store/revoke/status remain behind
edge auth and owner authentication.

### D5 — rate limits

Retrieval is keyed by `SHA256(license_key || NUL || machine_id)`, never by `build_id`.
The launch allowance is 12 requests per 60 seconds, enough for retries while containing
one credential/device pair. Shard retrieval fails closed if the rate-limit store is
unavailable; the general money-path limiter keeps its existing behavior.

### D6 — customer-visible failure states

The outer broker maps server codes to stable text and a retry/update action:

| Server/transport result | Customer text/action |
|---|---|
| transport/TLS failure | Can't reach Venice servers. Check your connection and Retry. |
| `license_revoked`, `license_inactive`, `license_expired`, `subscription_required`, `token_expired` | Your Venice access is not active. Subscribe at zaeorion.com or open a ticket. |
| `machine_mismatch` | This subscription is linked to another PC. Use `/hwid_reset`, then Retry. |
| `build_revoked`, `build_expired`, `build_not_found` | This Venice build is no longer available. Update Venice. |
| `rate_limited` | Too many launch attempts. Wait one minute, then Retry. |
| malformed/other | Venice could not validate this installation. Update Venice or open a ticket. |

No response includes the fragment, key, token, or raw internal exception.

### D7 — TLS

Both pack-time upload and runtime retrieval require HTTPS and TLS 1.2 or newer, disable
redirects, and use normal platform/public-CA hostname validation. The runtime does not
pin an API Gateway leaf certificate because AWS rotates leaf certificates. Pack-time
upload rejects an unexpected host unless an explicit staging override is supplied.

### D8 — rotation and revocation

Every release generates a new fragment with the OS CSPRNG and records the resulting
signing-stable `build_id` beside, not inside, the customer package. The coordinator
uploads the fragment before publication and aborts publication if upload fails. After
the new build passes the live acceptance gate and its grace period expires, the owner
revokes the prior `build_id`. A leaked/unsafe build is killed immediately with the same
revoke operation; the update manifest then points customers at the replacement build.

## Gap closure matrix

| Gap | State | Closure / remaining gate |
|---|---|---|
| 1. Gateway routes | OWNER STEP | Create four explicit routes in EXECUTE; no live change was made here. |
| 2. SSM encryption key | OWNER/COORDINATOR STEP | Create a 32-byte hex SecureString and verify Lambda read permission; no value is placed in this repo. |
| 3. Wire contract | CODED | Runtime contract is `{build_id,machine_id}` plus bearer token id headers; tests pin the exact field names. |
| 4. Edge auth | CODED | Only exact `POST /api/shard/retrieve` bypasses edge auth; bearer session validation replaces it. |
| 5. Identity/machine | BLOCKED | Requires the signed outer activation broker + DPAPI record before a clean install can launch. |
| 6. Per-build caps | CODED | Public release rows reject customer binding fields; rate limit is per license/machine. |
| 7. Tests | CODED | Backend handler/denial/audit tests and Lethe contract tests are listed below. |
| 8. Packer integration | CODED (fail-closed VERIFY) | `pack_lethe_release.py --server-shard` now ASSEMBLES the three-exe chain (packed payload + bootstrap-as-OrionNative.exe + unpacked broker), signs the customer manifest, and the gate VERIFIES every shard essential is present AND manifest-covered — failing closed on any gap (2026-09-20 hardening). The Ed25519-signed manifest is the broker's package-INTEGRITY record (owner is cert-free) but NOT a sufficient sole bootstrap trust anchor — an OS-enforced verifier outside the broker is an OPEN release blocker (red team 2026-09-20). Real end-to-end pack (built bootstrap/packed app on a pinned clean Lethe + the VeniceSigning key) remains the owner's rig step. Installer defaults to `release\orion-package-packed` and points launch/shortcuts/`orion://` at the broker. |
| 9. Acceptance | RIG REQUIRED | `scripts/accept_server_shard.ps1` records (a)-(f); (a)-(e) must pass live before ship. |

## Tests and evidence

Baseline before these changes:

- `python -m pytest tests/backend -q --basetemp=D:/NexusVision/pytest_tmp/shard_baseline`
  — 324 passed.
- `python -m pytest tests/test_bootstrap_safety_contracts.py tests/test_native_shard_runtime.py tests/test_cli.py -q --basetemp=D:/NexusVision/pytest_tmp/lethe_shard_baseline`
  — 33 passed, 1 skipped.
- A full Lethe run without `--basetemp` produced 824 passes, 4 skips and 132 setup
  errors, all rooted in the pre-existing denied `pytest-of-aaron` temp directory. It is
  an environment failure, not a shard regression.

Modified-tree evidence:

- `python -m pytest tests/backend -q --basetemp=D:/NexusVision/pytest_tmp/shard_backend_full`
  — **358 passed**.
- `python -m pytest tests -q --basetemp=D:/NexusVision/pytest_tmp/lethe_shard_full`
  in the Lethe repo — **961 passed, 6 skipped**.
- Focused bootstrap/shard/CLI lane — **40 passed, 1 skipped**; the bootstrap
  MSVC build and both native protocol vector tests pass.
- `python -m pytest tests -q --ignore=tests/discord --basetemp=...`
  — **3292 passed, 21 skipped, 8 xfailed, 2 xpassed, 5 unrelated failures**.
  The failures are concurrent controller lifecycle/UI drift, one framedump
  timing drop, and a GC log-drain race; none touches shard files.
- Lambda artifact: `D:\NexusVision\signing\orion-activate.zip`, 4.80 MiB,
  `CodeSha256=P2yRfM3K1ND7wTJY1E67rOj7sD3Z0aM25I56Ngg897M=`. It was not uploaded.
- After the owner closed Venice, StrictSecurity completed with exit **0**:
  Python **1659 passed, 2 skipped, 1 xfailed, 1 xpassed**; development native
  **24/24 passed**; production native **24/24 passed**; both package integrity
  checks and both security audits passed; final output `[orion] OK`.

## Acceptance gate

Run `scripts/accept_server_shard.ps1` as the owner on the rig. The script creates a
timestamped evidence directory and prompts for each observed result without reading or
printing any credential. Required live results:

1. Fresh install + existing Discord connect flow starts the packed app.
2. Network block produces the reachability message and no fallback.
3. Revoked build produces the update message.
4. Copy to machine B produces the machine-mismatch message.
5. A 20-minute run retains signed-lease/fire authority.
6. A second build id updates successfully; the first is revoked after grace.

The release decision requires (1)-(5) PASS; (6) is the update/rotation drill and must be
recorded before the first post-launch update.

## Rotation/revocation runbook

1. Pack to a private staging directory. Generate one fresh 32-byte fragment in memory.
2. Compute/record the packed PE content id as `build_id`.
3. Store `{build_id, encrypted_shard}` through the owner-authenticated store route.
4. Verify owner status returns the new build metadata without fragment material.
5. Run the owner acceptance gate against that exact build id.
6. Sign package/update manifests and publish only after the gate passes.
7. After the grace period, owner-revoke the previous build id and verify it returns the
   update-required message.
8. For an incident, revoke immediately, preserve audit/CloudWatch evidence, publish a
   mandatory replacement, and do not reuse the old fragment.

## EXECUTE — live sequence (not executed by this task)

1. **Coordinator or owner — SSM key.** Generate exactly 32 random bytes into a
   permission-restricted temporary file under `D:\NexusVision\signing`, encode as 64
   lowercase hex characters (the Lambda uses `bytes.fromhex`), and put it as SecureString
   `/orion/shard_encryption_key` in `us-east-1`. Delete the temporary file immediately.
   Confirm only the parameter name/type and that the Lambda role can read it; never print
   the value.
2. **Owner — API Gateway `v348t5hg3i`.** Attach integration `gpamg89` to:
   `POST /api/shard/retrieve`, `POST /api/shard/store`,
   `POST /api/admin/shard/revoke`, and both `GET` and `POST`
   `/api/admin/shard/status`. The extra status method is intentional and uses the same
   integration.
3. **Coordinator — Lambda.** Build `D:\NexusVision\signing\orion-activate.zip`, compare
   its recorded SHA-256, then deploy with `aws lambda update-function-code` using the
   currently observed `--revision-id`. Wait for success and run only non-secret denial
   probes first.
4. **Coordinator — pack.** After the signed activation broker has passed its clean-install
   test, run the shard-enabled command in `docs/LAUNCH_PACKING_CHECKLIST.md`. Supply owner,
   edge and TOTP values only through the process environment/secret manager; clear the
   process environment after the pack. Record `build_id`; abort if upload fails.
5. **Owner — rig acceptance.** Run `scripts/accept_server_shard.ps1`; require (a)-(e)
   PASS against live AWS.
6. **Coordinator — publish.** Only then build the installer from
   `release\orion-package-packed`, sign it, publish the signed update manifest, and post
   the verified installer in `#downloads`.

## Items not verifiable without the rig/live owner steps

- clean-install `orion://` activation broker behavior;
- actual runtime fragment fetch latency and plain-English dialog rendering;
- firewall/offline refusal with no fallback;
- live build revocation and machine-B copy refusal;
- 20-minute signed-lease continuity;
- second-build update and old-build grace-period revocation;
- API Gateway route attachment, Lambda IAM read of the new SSM parameter, and live
  CloudWatch audit/alert delivery.
