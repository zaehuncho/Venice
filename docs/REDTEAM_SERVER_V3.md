# Orion Licensing Server — Red-Team V3 (re-verification of the V2-fix re-implementation)

Read-only adversarial re-verification of the **uncommitted working-tree** re-implementation of the
six V2 findings + six NEW findings. Method: read the actual current code (not the summary), check
each test for whether it would still pass if the fix were reverted, verify the client/crypto
end-to-end, and hunt for new bugs. Scope files: `backend/lambda_function.py`,
`discord_launch/gumroad_webhook/lambda_function.py`,
`discord_launch/deployed_bundle/orion-sellhub-webhook/lambda_v6.py`, `tests/backend/*`,
`native_orion/src/LeaseGate.cpp`, `native_orion/src/LicenseClient.cpp`,
`docs/SERVER_DEPLOY_CHECKLIST.md`.

**Headline: this pass is real.** Unlike the V2 rewrite, the server-side holes are actually closed in
code and the tests mostly catch reverts. The ONE thing that looks green but is NOT yet exploitable-safe
is **NEW-1 end-to-end**: the server now signs the lease correctly, but the *client* half was not
wired (`leaseVerifyPublicKey()` still returns empty and `recordHeartbeatOk` does not verify), so the
MITM/cracked-client forge-lease exploit remains open until the client embeds the key. Two test cases
are effectively tautological (NEW-2 TOCTOU, NEW-3 timing-safety) and one new reliability bug was
introduced (Gumroad idempotency marker written before mint success → orphaned paid key on a transient
failure).

---

## Per-fix verdicts (12)

### CRIT-2 — /api/update signing oracle → **CLOSED**
`handle_post_update` (`backend/lambda_function.py:579-674`) now REQUIRES `signature` in the body
(`:599-603`), verifies it with the **public** key via `verify_manifest_signature` (`:119-132`, reads
`/orion/ed25519_public_key` with `WithDecryption=False`), and gates `artifact_url` through
`_artifact_url_allowed` (`:552-576`, HTTPS + owned host allow-list). `sign_manifest_ed25519` (`:106-117`)
and the private-key loader (`load_ed25519_private_key:88-94`) are **not called from any handler**.
Corroboration: `conftest.py` never sets `/orion/ed25519_private_key`, so any request path that loaded
it would break every test — it doesn't. Canonical bytes match the offline signer exactly
(`_manifest_canonical_bytes:102-104` == `conftest.sign_update_manifest_canonical:57-65`,
`sort_keys=False, separators=(",",":"), ensure_ascii=True`, same field order).
**Test quality: STRONG.** `test_tampered_signature_rejected` and `test_unsigned_manifest_rejected`
would FAIL against the old oracle (which minted a signature server-side);
`test_disallowed_artifact_host_rejected` uses a *validly-signed* evil URL and still expects 400 — a
revert with no allow-list would 200. Genuine.

### HIGH-2 — webhook valid-key replay → **CLOSED (Gumroad) / CLOSED (SellHub via order-scope)**
Gumroad `verify_license_key` sends `increment_uses_count="true"` (`gumroad:95`), rejects `uses>1`
(`:110-118`), and idempotency is keyed on the genuine `lickey:{license_key}` with a conditional put
(`:291-301`) — not the attacker-chosen `sale_id`. A forged ping replaying a real key with a fresh
`sale_id` is deduped. SellHub replay is now additionally killed server-side by `order_id` spend-once
in `handle_bot_provision`.
**Test quality: STRONG.** `test_replayed_key_mints_one_license` (same key, fresh sale_id → exactly 1
`provision_calls`) fails against sale_id-keyed idempotency.
**NEW BUG (MED) — see B1 below:** the `lickey` marker is written BEFORE the provision call succeeds.

### HIGH-3 — shared secret + bot routes off edge auth + no order scope → **PARTIAL (mostly closed)**
- Per-consumer secrets: `require_bot` iterates `BOT_SECRET_SSM_NAMES` (`:30-34, 182-197`), timing-safe,
  no early-out. Independent rotation confirmed.
- Edge auth on `/api/bot/*`: the exemption is gone — the router calls `require_edge_auth` for ALL
  routes before dispatch (`:1548`). Webhooks now attach `X-Edge-Auth` (`gumroad:187`, `sellhub:150`).
- Order scope: `handle_bot_provision` REQUIRES `order_id` and claims it spend-once atomically
  (`:1515-1520` → `_claim_order:1333-1346`, conditional `attribute_not_exists`) before minting.
- **Residual:** `handle_bot_deliver` (`:1454-1493`) still mints any plan incl. `lifetime` with NO
  `order_id` (order_id is optional there). It is the admin manual-mint path, now gated behind BOTH
  edge-auth AND a bot secret, so "mint a lifetime key without a real order" is no longer a
  single-secret exploit — but it is still possible for anyone holding both secrets. Downgraded from
  HARD-open to defense-in-depth PARTIAL.
**Test quality: STRONG** for provision (`test_provision_mints_once_per_order` asserts exactly one real
row on replay — fails if `_claim_order` reverted; `test_provision_requires_order_id`;
`test_provision_denied_without_bot_secret`). No test exercises the deliver-without-order residual.

### HIGH-4 — edge auth fail-open → **CLOSED**
`require_edge_auth` (`:1239-1258`): absent secret → `err("forbidden",403)` (fail CLOSED, `:1248-1252`);
no `EDGE_AUTH_ENFORCE` gate (always enforced); `hmac.compare_digest` (`:1255`); applied to every route
(`:1548`, incl. `/api/version` and GET `/api/update`).
**Test quality: STRONG.** `test_missing_edge_header_denied`, `test_fail_closed_when_secret_absent`
(deletes the SSM param → 403), and `test_bot_route_now_behind_edge_auth` all fail against the old
fail-open/exempt behavior.
**Caveat (B4):** because edge auth is now on `/api/version` and GET `/api/update` too, any client,
health-check, or update-poller that does NOT traverse the Cloudflare worker (which injects the header)
is now 403'd. Confirm ALL legitimate callers present `X-Edge-Auth`.

### MED-1 — activate rate-limit + nonce replay → **CLOSED**
`handle_activate` reads nonce+timestamp from body or `X-Orion-Request-*` headers (`:323-338`), rejects
missing fields (`replay_fields_required`), stale timestamps (`TS_SKEW_LIMIT=300`), and replays via
`check_nonce("activate:"+nonce)` conditional put (`:971-982`). Fixed-window rate limit
`rate_limit_ok` on key + IP (`:316`, atomic `ADD`, `:228-247`).
**False-deny check: PASS.** The real client sends both the headers and the body fields
(`LicenseClient.cpp:233-244`), so legitimate activations are not broken.
**Test quality: STRONG.** `test_nonce_replay_rejected`, `test_stale_timestamp_rejected`,
`test_missing_replay_fields_rejected` all fail against the V2 code (which ignored these fields).

### MED-2 — staff machine-binding → **CLOSED**
`require_staff` (`:940-943`): `if bound: if not machine_id or not compare_digest(machine_id, bound):
reject`. The `machine_id and ...` short-circuit that let a header-less stolen token skip binding is
gone; tokens are always issued machine-bound (`issue_staff_token:882-901`).
**Test quality: STRONG.** `test_missing_machine_header_rejected` (no `X-Machine-Id` → 403) fails
against the old short-circuit.

### NEW-1 — heartbeat lease signing → **SERVER CLOSED / END-TO-END STILL-OPEN**
Server: `sign_lease` (`:145-153`) signs `f"{license_key.strip().upper()}:{machine_id.strip()}:{int(
lease_expires_at)}"` with a **dedicated** Ed25519 key (`/orion/lease_signing_key`), base64url no pad;
returned in `handle_validate` (`:1312-1314`). This EXACTLY matches the client verifier
`LeaseGate::verifyLeaseSignature` (`native_orion/src/LeaseGate.cpp:138-142`, same
`license:machine:expires` string, `Base64UrlEncoding|OmitTrailingEquals`). The deploy-checklist
keypair is internally consistent — I derived the public key from the checklist private PEM and it
equals `hckqeE6FqT9jHpOLVvJRl6r1C70/RhjmMCdIKJq9nOw=` verbatim.
**BUT the client half is NOT wired:** `LeaseGate::leaseVerifyPublicKey()` still returns an empty
`QByteArray` (`LeaseGate.cpp:145-152`), and `recordHeartbeatOk` (`:54-64`) blindly trusts
`leaseExpiresAtEpochS` — it never calls `verifyLeaseSignature`. So the V2 NEW-1 exploit (a MITM /
cracked client returns `{"ok":true,"lease_expires_at":<far future>}` → `fireAllowed()` passes
indefinitely) is **still open** until the client embeds the key and enforces the signature.
Secondary V2 point also unaddressed: `handle_validate` still requires **no session token**, so anyone
with a known key+machine can harvest signed leases.
**Test quality: correct but scope-limited.** `test_forged_lease_expiry_does_not_verify` /
`test_signature_binds_machine_id` genuinely assert the crypto (independent public key, real verify) —
non-tautological for the *server* property — but by construction they CANNOT catch the un-wired client,
which is where the actual control lives. **This is the #1 "green but still exploitable" item.**

### NEW-2 — device-cap TOCTOU → **CLOSED (code) / TEST TAUTOLOGICAL**
The atomic conditional write is present: `update_item` with `ADD activations :one` +
`ConditionExpression` (`:429-441`). DynamoDB evaluates the condition atomically against current item
state, so two concurrent binds on a fresh `max_devices=1` key cannot both win (verified by reasoning:
the second serialized write sees `activations=1`, `1<1` false, all OR-terms false → rejected).
**Test quality: WEAK / effectively tautological.** `test_conditional_write_backstops_toctou`
hard-codes its OWN copy of the condition string and drives DynamoDB directly — it tests DynamoDB's
semantics, not that the *handler* uses that condition. `test_cap_holds_second_machine_rejected` is
satisfied by the pre-check at `:403-406` alone. **Reverting the handler's `ConditionExpression` to a
non-atomic `SET activations = :a+1` would leave BOTH tests green.** The atomic property is not guarded
by a revert-catching test through the handler.
Minor code note: the `machine_id = :m` OR-term means a legit machine re-activating repeatedly inflates
`activations` unboundedly. Harmless (inflation only makes `activations < max` *less* true → fails
closed), but the counter becomes meaningless.

### NEW-3 — Gumroad URL token timing-safe → **CLOSED (code) / TEST TAUTOLOGICAL**
`gumroad:207` uses `hmac.compare_digest(token, expected)`.
**Test quality: TAUTOLOGICAL.** `test_wrong_url_token_rejected` only asserts a wrong token → 403,
which the old `!=` did equally. It would NOT catch a revert to `!=`. (LOW severity regardless.)

### NEW-4 — SellHub prefix-strip → **CLOSED**
`lambda_v6:65-67` uses `if sig.startswith("sha256="): sig = sig[len("sha256="):]` — a true prefix strip.
**Test quality: STRONG.** `test_digest_leading_hex_not_eaten` searches for a digest whose first hex
char is in `"sha256="` and asserts it verifies — this fails against the old `lstrip("sha256=")`.

### NEW-5 — handle_validate json.loads guarded → **CLOSED**
`:1264-1267` wraps `json.loads` and returns `err("invalid_json",400)`. (No dedicated test; trivially
correct.)

### NEW-6 — webhook error text leak → **CLOSED**
Gumroad returns bare `{"error":"revoke_failed"}` / `{"error":"provision_failed"}` (`:275,308`);
SellHub returns bare `{"error":"revoke_failed"}` / `{"error":"provision_failed"}` (`:252,289`).
**Test quality: STRONG.** `test_provision_error_body_is_generic` asserts the exact body and `"detail"
not in parsed` — fails against a `detail=str(e)` revert.

---

## New bugs introduced by this change (ranked)

### B1 — MED (reliability): Gumroad `lickey` idempotency marker written BEFORE the mint succeeds
`gumroad:291-301` writes `lickey:{license_key}` (conditional put, status "minted") and THEN calls
`provision_license` (`:303-308`). On a transient provision failure (returns 500), the marker persists
and is never cleared, AND the earlier `event_key` marker (`:241-250`) is also set. Gumroad retries the
same ping → `event_key` dedupe → `duplicate_event` 200 → provision is **never re-attempted**. Net: the
customer paid, the license was never minted, and the retry is silently swallowed. The `event_key`
part is pre-existing, but the new `lickey` marker compounds "mark-before-success." SellHub has the
same shape with its `event_id` marker (`lambda_v6:216-225` written before provision). It does NOT
over-block legitimate *distinct* purchases (each Gumroad sale has a distinct key). **Fix:** write the
success marker only after provision (and DM) succeed, or clear it on failure.

### B2 — HIGH residual (not new, but not closed): NEW-1 client half un-wired
See NEW-1. The lease forge exploit is live until `leaseVerifyPublicKey()` returns the 32-byte key and
`recordHeartbeatOk` rejects on `verifyLeaseSignature` failure. Server signing is backward-compatible,
so this can be finished client-side without a server change. Top priority to actually realize the fix.

### B3 — LOW: `_claim_order` loser-race returns an empty license_key
`_claim_order` (`:1333-1346`): the winner claims the marker, then `_bind_order_license` writes
`minted_license_key` as a *separate* update (`:1533`). A truly concurrent duplicate that loses the
conditional put reads the marker before the winner binds → `minted_license_key` is "" →
`handle_bot_provision` returns `{"ok":true,"license_key":"","duplicate_order":true}`. No double-mint
(safe), but the caller can get an empty key on a race. Providers don't fire same-order concurrently in
practice. **Fix:** bind the minted key into the marker's own conditional put, or re-read after a short
backoff.

### B4 — LOW/caveat: edge auth now covers `/api/version` and GET `/api/update`
Not a vulnerability — a potential false-deny. Any legitimate caller that doesn't go through the
Cloudflare worker (health checks, direct update polls) is now 403'd. Confirm the worker injects
`X-Edge-Auth` on every proxied route (the checklist §5 says it must).

---

## Tests that would still pass if the fix were reverted (the real check)
- **NEW-2 `test_conditional_write_backstops_toctou`** — hard-codes its own condition string; a revert
  of the handler's `ConditionExpression` leaves it (and the pre-check-satisfied cap test) green.
- **NEW-3 `test_wrong_url_token_rejected`** — asserts only rejection; a revert from `compare_digest`
  to `!=` still rejects, so the timing-safety property is untested.
- **NEW-1** backend tests pass regardless of the un-wired client — they assert only the server
  property, which is genuine, but they give false confidence that the end-to-end control is closed.

All other tests (CRIT-2, HIGH-2, HIGH-3, HIGH-4, MED-1, MED-2, NEW-4, NEW-6) are genuine
revert-catchers — I traced a concrete revert that would flip each red.
