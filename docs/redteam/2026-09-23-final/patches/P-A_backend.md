# P-A — backend patch for the rc1 BLOCKED findings (2026-09-23)

Owner of: `backend/lambda_function.py`, `tests/backend/**`, `website/src/worker.js`,
`website/tests/worker.test.mjs`. No deploys, no live calls, no commits, no secrets printed.

Files changed:
- `backend/lambda_function.py` (+~560 / -~70)
- `tests/backend/test_rc1_pa_backend.py` (new, 55 tests)
- `website/src/worker.js` (+14: forwards `stripe_event_id`)
- `website/tests/worker.test.mjs` (+3 tests, 2 existing body assertions extended with `stripe_event_id`)

Pre-patch copies: scratchpad `lambda_function.before.py`, `worker.before.js`, `worker.test.before.mjs`
(both files were clean against HEAD `ea8729b` before this patch).

## Evidence summary

| Run | Command | Result |
|---|---|---|
| Baseline, before any edit | `.venv\Scripts\python.exe -B -m pytest tests/backend -q -p no:cacheprovider --basetemp C:/Users/aaron/AppData/Local/Temp/pa-backend` | **375 passed**, `A7_OFFLINE_GUARD_BLOCKS 0`, exit 0 |
| New tests, before the fix | `... pytest tests/backend/test_rc1_pa_backend.py ...` | **48 failed, 7 passed** (the 7 are controls that already held: wrong machine, disabled owner, v1 lease unchanged, the staff-licence reset that already had an attempt row, etc.) |
| New tests, after the fix | same | **55 passed** |
| Full backend suite, after | same as baseline | **430 passed** (375 + 55), `A7_OFFLINE_GUARD_BLOCKS 0`, exit 0 |
| Codex's CSEC fixture (`codex-security-rc1-.../tests/backend/test_codex_security_fixture.py`, which asserts the *exploit succeeds*), temp copy run against the patched code, then deleted | `... pytest <copy> --tb=line` | **4 failed, 1 passed**: kill-without-audit now 503, warm-cache lease now 503, both owner-bearer actions now 403. The one that still "passes" is CSEC-09 (two-device rebind), which is outside this lane |
| Worker, new tests before the Worker fix | `node --test tests/worker.test.mjs` (in `website/`) | **tests 50, pass 48, fail 2** (the two forwarding tests. The 409→500 test already held) |
| Worker, after | same | **tests 50, pass 50, fail 0**, exit 0 |

## RT-CRIT-01 / CSEC-12: owner step-up

**Change.** `owner_step_up()` (`lambda_function.py` ~3753) gates each owner-sensitive action. It uses the same code path whichever authenticator resolved the actor (break-glass secret or owner-role staff bearer):
1. Owner TOTP required: needs a code in `X-Orion-Admin-TOTP` (or body `step_up_code`). A code that fails to verify is denied with 403 and an `owner.step_up_denied` audit row.
2. REQUIRED attempt audit (`audit_attempt`). If that write fails: 503 `audit_unavailable`, the code is not burned, and nothing changes.
3. The matched TOTP time step is burned in `orion-nonces` (`owner_totp_used:<counter>`, conditional put). A replayed code gets 403 `totp_replayed` plus an audit row. If the store is down: 503 `step_up_unavailable`.
4. Owner TOTP **not** required: there is no second factor to step up to. The break-glass secret may act, because it is the possession factor. A staff bearer gets 403 `step_up_required`.

**Actions that now need step-up:** `totp_enroll` (this used to be able to overwrite the SSM secret silently), `totp_confirm`, `totp_disable` and `rotate_admin_secret`. Also `config set` of `owner_totp_required`, `owner_ip_allowlist` or `alerts` (the `set` route was a second TOTP-off bypass not in the report), releasing the global kill (`/api/admin/unkill` global, or `config set global_kill enabled=false`), and staff `create` with role owner. Any staff action on an owner row, or `set_role` to owner, also needs it, since `reissue_enrollment` on an owner row mints a new owner. Engaging the kill stays a single step because that is the safe direction.

**Login possession factor (contained, fixed).** `handle_staff_login` (~1631): when owner TOTP is required, an **owner-role** login must also present a fresh single-use code (body `totp_code` or `X-Orion-Admin-TOTP`). A failed login writes a `staff.login_denied` audit row. Support and admin logins are unchanged.

**Other fail-open closed on the way.** `owner_totp_required` and `owner_ip_allowlist` used to go through `config_get()`. On a DynamoDB error that returned the *default* (TOTP off, allow-all) and cached it for 15 s. They now use `config_get_strict()` (~3637): uncached and `ConsistentRead`. A read error raises `ConfigUnavailable`, which the router answers with 503 `config_unavailable`.

**Tests** (`TestOwnerStepUp`, 16 cases): stolen bearer without a code (disable and rotate); wrong code and expired code (step -3); replayed code; wrong machine; disabled owner; audit outage → 503 with TOTP and admin secret unchanged; a valid code succeeds with attempt row + `-ok` completion row; owner login needs TOTP while support login does not; the `config set` bypass; `totp_enroll` overwrite; a staff owner with no TOTP → `step_up_required` while break-glass still rotates; owner promotion; kill release needs step-up while engaging does not; security-config read failure → 503.

**Residual risk.**
- Owner-role staff tokens issued **before** deploy stay valid for up to 7 days (`STAFF_TOKEN_TTL`). The login factor does not retro-revoke them, but step-up still guards every takeover action. See deploy step 5.
- While owner TOTP is **not** required, an owner-role bearer is still single-factor for the *non*-step-up owner routes: licence actions, kill engage, metrics and audit reads. The fix is to enrol TOTP. The machine ID is still a claimed string, not device attestation. A real device-bound key (for example, a signed login challenge) is a client + backend project and was not done here.
- For break-glass with TOTP required, the gate code is burned by the first sensitive action. Two sensitive actions inside the same 30 s step need two different codes (UX only).
- **Client impact (not my files; proposals):**
  - `native_orion/src/AdminToolController.cpp` `applyAuth()`: for `AuthKind::Owner` on the staff-token branch, also send `X-Orion-Admin-TOTP` when `ownerTotp_` is set. Without this, OrionOwner/Staff with an owner-role bearer gets `totp_required` on the step-up actions.
  - `staffLogin()`: add a TOTP field and send `"totp_code"` in the body. Without it, an owner-role login fails with `totp_required` whenever TOTP is required. The break-glass owner build is unaffected.
  - `tools/admin/orion_admin.py` `build_auth_headers()`: send `X-Orion-Admin-TOTP` on the staff-token branch too.
  - All clients should surface `step_up_required`, `totp_replayed` and `config_unavailable` as plain messages.
- Side fix: `staff create` without a `discord_user_id` wrote `""` into the `discord_user_id-index` GSI key. DynamoDB and moto both reject that, so every such create returned 500. The attribute is now omitted when empty.

## RT-CRIT-02 / CSEC-13: unknown kill state

**Change.** `get_global_kill()` (~553) no longer reuses `_LAST_GLOBAL_KILL`. That name is kept for diagnostics only and is never used to decide anything. Every read is a `ConsistentRead` GetItem, and any failure returns `(True, "kill_state_unavailable")`. The new `kill_denial()` maps that to **503 `kill_state_unavailable`** (`retryable: true`) on `/api/license/check`, activate/redeem and shard retrieve.

This code is deliberately **not** in `isLicenseKillCode` (`LicenseClient.cpp:249`). The launcher keeps only its already-signed, bounded lease, retries, and fails closed at expiry. Before this patch, even the *cold* failure returned `service_disabled`, which the launcher treats as a kill verdict and signs out on, so an outage logged customers out. An engaged kill still returns `service_disabled` exactly as before. No versioned cache was added: fail-closed could not be proven for one, and the read is a single GetItem.

**Tests** (`TestKillStateFailClosed`): warm "off" → owner engages kill via `/api/admin/kill` → selective read failure gives 503 `kill_state_unavailable` with no `lease_sig`/`lease_expires_at` and a code outside the client kill set; recovery gives `service_disabled`, and unkill then returns a lease again. Also: cold outage, activation under an outage (no bind, no token), and the read being `ConsistentRead`.

**Residual.** Tested at the server response only. The actual native fire gate and the client-side handling of the new code were not exercised here; that is P-owned client or VM work. `version_gate`/MOTD reads still fall back permissively on error. That was left alone because those are non-security availability reads.

## RT-HIGH-07 / CSEC-10: required pre-mutation audit

**Change.** `audit_attempt()` (~506) writes `<action>.attempt` with `required=True`. `AuditUnavailable` propagates to the new `lambda_handler` wrapper (~5596), which returns 503 `audit_unavailable` before anything is mutated. `audit_complete()` (~512) writes the completion row with the deterministic id `<attempt_id>-ok` (or `-<result>`), so a retried completion replaces the row rather than duplicating it. The completion write is retried once and never undoes a mutation that has already happened; the attempt row is the durable record.

**Routes covered** (attempt row before mutation):
- `/api/admin/kill` (licence and global)
- `/api/admin/unkill` (licence and global)
- `/api/admin/provision`
- `/api/admin/license` and `/api/staff/license`: create, blacklist and unblacklist (newly covered); the other actions already had the CX-014 attempt row
- `/api/admin/staff`: create, disable, enable, set_role, set_caps, reset_machine, reissue_enrollment
- `/api/admin/config`: all totp actions, rotate and set
- `/api/admin/shard/revoke` and `/api/shard/store`
- `/api/update` POST (manifest publish)
- `/api/bot/deliver` (staff mint; placed before the order marker is spent)
- every HWID reset, via `apply_hwid_reset`: staff, owner and Discord self-service

**Tests** (`TestRequiredDestructiveAudit`): 20 parametrised routes. Each one fails the audit table's PUT, asserts 503 `audit_unavailable`, then asserts a snapshot of the affected state is unchanged. Two more tests check attempt → `-ok` completion ordering and idempotency, and that a completion write failing after the attempt still returns 200 with the attempt row present.

**Residual.**
- Webhook-originated revocations (`/api/bot/chargeback`) are *not* refused on an audit outage. Refusing a revoke would be the fail-open direction, and they already write a (non-required) row. Say so if you want them required; Stripe would then retry until the audit table recovers.
- Customer trial claims are not destructive staff actions and were not changed.
- The owner-alert DM is still best effort.

## RT-LOW-04 / CL3-F6-002: Stripe event.id dedup

**Change.**
- Worker: `stripeEventRef()` forwards the signature-verified `event.id` (pattern `evt_[A-Za-z0-9_]`) as `stripe_event_id` on every provision and chargeback call.
- Backend: `with_stripe_event_dedup()` (~5487) wraps `handle_bot_provision` and `handle_bot_chargeback`. It accepts ids from the Worker consumer only (others → 403) and rejects a malformed id with 400 `invalid_event_id`. The claim marker `stripe_evt:<id>` in `orion-nonces` goes processing → done.
  - A done event is a **200 `duplicate_event` no-op**: no revoke, no audit row, no owner alert, no DM.
  - An in-flight duplicate gets 409 `event_in_progress`. The Worker returns 500, so Stripe retries later.
  - A non-2xx outcome deletes the claim so Stripe's retry reprocesses.
  - A claim stuck in processing goes stale after 120 s.
  - If the event store is down: 503.
- Also, without an event id: a chargeback for a subscription or key that is already revoked now returns a quiet 200 `already_revoked`. Before, it re-alerted on the key path, and on the Discord path it returned 404, which made Stripe retry for 3 days.

Entitlement idempotency is unchanged underneath (ORDER# markers, revoke state, `notified_at`).

**Tests:** backend `TestStripeEventDedup` (7) covers the replay no-op (one ok audit row, one alert), replay without an id, a failed attempt releasing the claim, in-progress 409, the Worker-only id, a malformed id, and provision replay minting once with one DM. Worker: 3 new tests.

**Residual.** The Worker still repeats its Stripe API reads on a replay, because dedup sits at the backend. An edge KV seen-set would avoid that but needs a new binding and namespace.

## RT-MED-12 / CSEC-06: heartbeat lease freshness

**Server half implemented, backward compatible.** `/api/license/check` accepts an optional `lease_nonce` (16–128 chars, `[A-Za-z0-9_-]`; anything else → 400 `invalid_lease_nonce`). When it is present, the response adds:
- `lease_nonce` (echoed)
- `lease_issued_at` (server epoch seconds)
- `lease_sig_version: 2`
- `lease_sig_v2`: an Ed25519 signature, with the **same lease key**, over:

```
orion-lease-v2\n<LICENSE_KEY upper/trim>\n<machine_id trim>\n<lease_expires_at>\n<lease_issued_at>\n<lease_nonce>
```

v1 `lease_sig` is unchanged and still sent, so shipped clients keep working. The domain tag (`orion-lease-v2` is lower case; v1 messages start with an upper-case key) stops a v2 signature from ever verifying as v1. Tests: `TestLeaseNonceBinding` (6).

**Client change needed (proposal; `LicenseClient.cpp` / `LeaseGate.cpp` are not in this lane):**
1. `licenseCheckRequestBody()`: add `lease_nonce` = 32 random bytes as base64url (or a UUID without braces, 36 chars), created per request and kept with the in-flight request.
2. `parseLicenseCheckResponse()`: read `lease_sig_v2`, `lease_nonce`, `lease_issued_at` and `lease_sig_version`.
3. Add `LeaseGate::verifyLeaseSignatureV2(pub, key, machine, expires, issuedAt, nonce, sig)`, building the exact message above.
4. Accept the heartbeat only if all of the following hold. Otherwise treat it as a transport failure (non-kill, keep the existing lease, retry).
   - the echoed nonce equals the one sent;
   - the v2 signature verifies;
   - `lease_issued_at` ≥ the last accepted `lease_issued_at`;
   - `|lease_issued_at − local send time| ≤ 300 s` (same skew as activation);
   - `lease_expires_at − lease_issued_at ≤ LEASE_TTL_S` + slack.
5. Once the backend is deployed and a client ships that requires v2, reject any response that lacks `lease_sig_v2`, so an attacker cannot downgrade to v1.

This removes replay of old responses before expiry. A backend "session epoch" was not added: with the nonce binding it adds nothing a replayer could not already be denied. Revocation freshness is still bounded by `LEASE_TTL_S`.

## DEPLOY NOTES (owner)

No new tables, GSIs, env vars or SSM parameters are needed.

1. **DynamoDB `orion-nonces`**: now also stores `owner_totp_used:<counter>` (about 2 min life) and `stripe_evt:<evt_id>` (30 days).
   - Rows carry both `expires` and `ttl`. `docs/SERVER_HANDOFF.md` says the table's TTL attribute is `ttl`, but the code has always written only `expires`, so existing nonce rows never expired. Confirm with `aws dynamodb describe-time-to-live --table-name orion-nonces` and make sure TTL is on `ttl`.
   - The Lambda role needs `dynamodb:GetItem`, `PutItem` (conditional) and `DeleteItem` on `orion-nonces`. `DeleteItem` is new; check the policy.
2. **`orion-config` / `orion-audit`**: `ConsistentRead` GetItem on `orion-config` is new (same `GetItem` permission). Audit rows now use ids `<ULID>-ok`; the `day-ts-index` reader is unaffected.
3. **Deploy order:**
   1. Lambda first, from this tree. The patched backend accepts both old and new Worker payloads.
   2. Then the Worker. The new Worker's extra `stripe_event_id` field is ignored by an old backend, so either order is safe, but backend-first gets dedup immediately.
   3. Then the OrionOwner/Staff client TOTP changes above.
   4. Then the LicenseClient v2 change, as a later client release.
4. **Before deploying, if owner TOTP is required in production:** the owner-role staff login now needs a code that the current Staff build cannot send. Use the break-glass owner build (secret + TOTP) until the client proposal ships.
5. **After deploy:**
   - Revoke existing owner-role staff tokens (`/api/admin/staff` `reset_machine` on the owner row, which now needs step-up) so every owner bearer is re-minted with the factor.
   - If TOTP is not enrolled, enrol it with break-glass: `totp_enroll` then `totp_confirm`.
   - Lost-device recovery: delete SSM `/orion/owner_totp_secret` out of band (AWS console), which makes the `owner_totp_required` check fail closed with `totp_not_provisioned`. Then set config `owner_totp_required=false` directly in `orion-config` and re-enrol via break-glass. Record this in the incident runbook.
6. **Monitoring:** alert on `owner.step_up_denied`, `staff.login_denied`, `*.attempt` rows without a matching `-ok` completion, and log lines `global_kill read failed`, `destructive mutation refused, audit unavailable` and `security config unavailable`.
7. Re-run StrictSecurity on the rc2 unit. This patch touches backend/API, staff/admin and license/auth.

## Follow-up: re-enrolment and confirm replay (from P-F, 2026-09-23)

P-F reported two backend issues in the step-up flow. Both are fixed in `backend/lambda_function.py`.

1. **Re-enrolment killed the current factor.** `totp_enroll` overwrote the ACTIVE secret `/orion/owner_totp_secret` while `owner_totp_required` stayed true. From that moment the break-glass session code, the gate, owner login and step-up all needed the unconfirmed new secret.
   - **Fix:** `totp_enroll` now writes the new secret to a staged SSM parameter, **`/orion/owner_totp_pending_secret`**, and the response adds `"pending": true`. The active secret keeps working until `totp_confirm` proves the new one. Confirm then promotes the staged secret to active and deletes the staged parameter.
   - A wrong confirm code leaves the old factor active.
   - `totp_disable` also clears any staged secret.
   - The config snapshot adds `owner_totp_pending`.
2. **Confirm failed as a replay.** `totp_confirm` burned the step-up code *before* checking the confirm code, so two codes from the same 30 s step collided as `totp_replayed`.
   - **Fix:** confirm no longer does a separate step-up burn. The body `code` is the proof: it verifies against the staged secret (whose enrolment already required the current factor) or, with nothing staged, against the active secret. It is burned once, after the REQUIRED attempt audit.
   - Burned-step keys are now namespaced per secret (`owner_totp_used:<sha256-tag>:<counter>`), so steps from the old and new secrets never collide.
   - A header code sent with confirm is ignored and not burned. Clients may keep sending it, but no longer need to prompt for a separate code (note for P-F).

**Tests.** New class `TestTotpReEnrolment` (5 tests) in `tests/backend/test_rc1_pa_backend.py`.
- Before the fix: **4 failed, 1 passed**. The one that passed is a control: a stolen bearer cannot confirm without the new secret.
- After the fix: all pass.
- The 5 tests cover:
  - the session code surviving a re-enrolment, then the switch-over on confirm (old code refused, new code accepted);
  - a wrong confirm code keeping the old factor;
  - one code for header and body in one window, which then stays single-use;
  - the P-F console flow: enrol step-up and confirm within seconds;
  - the stolen-bearer control.

**Existing test adjusted.** `tests/backend/test_admin_v2.py::TestOwnerTotp::test_enroll_confirm_then_required` reused the confirm's code for `totp_disable` in the same 30 s step. Under single-use codes that is a replay, which is correct to refuse. The test now uses the next step's code. The earlier 430/430 pass most likely straddled a step boundary.

**Full suite:** `tests/backend` **435 passed**, `A7_OFFLINE_GUARD_BLOCKS 0`, exit 0. P-F's `tests/test_admin_owner_step_up.py`: **73 passed** (its backend-parity guards still hold).

**Deploy note (addition).** New SSM SecureString **`/orion/owner_totp_pending_secret`**, created by the first re-enrolment. The Lambda role needs `ssm:GetParameter`, `ssm:PutParameter` and `ssm:DeleteParameter` on it, with KMS decrypt/encrypt as for `/orion/owner_totp_secret`.
- If the role lacks `PutParameter` on the new name, `totp_enroll` returns 500 `ssm_write_failed` and nothing changes.
- A staged secret has no expiry. An abandoned enrolment stays staged, inert (it authorises nothing but its own confirm), until the next enrol or disable overwrites or clears it.
- **Lost-device runbook:** also delete the staged parameter.
