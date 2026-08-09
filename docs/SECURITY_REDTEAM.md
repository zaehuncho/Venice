# Orion — Licensing & Anti-Piracy Red-Team (2026-07-09)

Adversarial read-only review of `backend/lambda_function.py`, `gumroad_webhook/lambda_function.py`, `orion_bot.py`, `orion_worker.js`, and the client license path (`SecurityManager/LicenseClient/OrionAppController/UpdateManifest/updater_main/NetworkSecurity`). Ranked severity × exploitability. **Headline: the server is not the gate — the client is trusted.** The server crypto is mostly well-built but protects an object (the license) the client never actually needs to run.

## 🔴 CRIT-1 — The client is the gate; automation runs with no valid server entitlement (High exploitability)
`authenticated_` is a plain client bool set true on `activationFinished` (`OrionAppController.cpp:1150`); the HMAC session token from `/api/activate` is **discarded** (only a `token_present` bool cached, `:1158`). Fire is gated on `automationSecurityAllowed()` (`:5875`) which checks only `securityLockActive_` — NOT `authenticated_`, NOT any server state. **A cracked binary that forces `authenticated_=true` (or NOPs the check) runs the full aimbot offline forever, no license, no network.**
**FIX (Day-5 client + server):** bind the AutomationEngine's ability to FIRE to a short-lived server-signed LEASE (reuse the existing `HMAC(token_id:license_key:expires)` token) re-presented on the heartbeat; gate `automation_.process()` on a verified-unexpired lease, not a local bool. A native client is never fully uncrackable — the goal is to make the *useful* part require a revocable server round-trip.

## 🔴 CRIT-2 — /api/update is a signing ORACLE → admin-secret leak = signed RCE on every client (Low-Med)
`/api/update` (guarded only by one static `require_admin` SSM secret) signs whatever the caller supplies incl. attacker-chosen `artifact_url`+`sha256` (`:404-477`); clients trust any manifest that verifies (`updater_main.cpp:225`). One leak of `/orion/admin_secret` → signed malicious manifest → auto-applied trojan → RCE on all installs. No rate-limit/lockout on `/api/admin/*`.
**FIX (07-15 server):** sign releases OFFLINE with an air-gapped key (Lambda only STORES a pre-signed manifest, never IS the oracle — the new package pipeline already emits `update_manifest.unsigned.json` for exactly this); allow-list `artifact_url`; per-operator admin creds + rate-limit + alert on `/api/admin/*`.

## 🟠 HIGH-1 — Machine-binding is client-asserted, trivially spoofable/shareable (High)
`machine_id` is computed client-side + sent verbatim; server can't verify. Ship one `license_key` + one fixed `machine_id` string → every cracked client sends the same pair → `activations>=max_devices` never trips → unlimited concurrent installs on one license.
**FIX:** can't enforce HW binding from an untrusted client — mitigate server-side: rate-limit distinct machine_ids/key, alert on impossible-travel/many-IP per key, cap concurrent live LEASES per license (ties into CRIT-1).

## 🟠 HIGH-2 — Gumroad webhook: valid-key replay mints unlimited licenses (Med, needs URL token)
Idempotency keyed on attacker-controlled `sale_id` (`:208`); `verify_license_key` uses `increment_uses_count="false"` (`:91`) → a real key verifies unlimited times; no dedupe on `license_key` itself. Buy the cheapest tier once → with the Ping URL token, POST forged pings (fresh `sale_id`, correct constant `seller_id`, the genuine key) → each mints a new license + DMs it. One $ → unlimited keys of that tier.
**FIX (07-15 server):** idempotency on `license_key` (one backend license per Gumroad key EVER); `increment_uses_count="true"` + reject uses>1; treat the URL token as rotatable, keep out of logs.

## 🟠 HIGH-3 — Shared `bot_service_secret` mints unlimited licenses; wide leak surface (Med)
`/api/bot/deliver` + `/provision` mint arbitrary licenses (incl. lifetime `expiry=0`) guarded only by one static secret COPIED into 3 environments (Discord bot host, Cloudflare Worker `orion_worker.js:48`, webhook Lambda). Any one leak = unlimited free lifetime keys. `/api/bot/*` is exempt from edge auth (`:1272`) + the origin is hardcoded → reachable directly.
**FIX:** per-consumer secrets (webhook vs bot vs worker), rotate independently; scope deliver/provision to a verified-unspent order_id; put bot routes behind edge auth / IP allow-list.

## 🟠 HIGH-4 — Edge auth FAILS OPEN + bypassable at origin (Med)
`require_edge_auth` returns allow when the secret is missing + only blocks if `EDGE_AUTH_ENFORCE` set (else log-only). Origin execute-api URL is public+hardcoded → attacker hits Lambda directly, bypassing Cloudflare rate-limiting. This is the delivery vector making CRIT-2/HIGH-2/HIGH-3/MED-1 reachable.
**FIX (07-15):** enforce edge auth, fail CLOSED when secret absent, restrict API-GW to Cloudflare source IPs / require a CF-injected header.

## 🟡 MED — rate-limiting / replay / staff / TLS
- **MED-1:** no server-side rate-limit on activate/redeem/validate/admin/bot; the client's 1250ms guard is irrelevant to a direct caller. Also the activate nonce+timestamp fields are SENT but `handle_activate` never reads them → activation has zero replay protection despite looking like it does. FIX: API-GW throttling + actually enforce the nonce (or delete the dead fields).
- **MED-2:** staff machine-binding only checked IF `X-Machine-Id` sent → a stolen staff token used without the header skips binding. FIX: require the header + enforce unconditionally.
- **MED-3:** TLS pin is advisory (fails open on leaf mismatch, by design for CF renewals) → no real protection. FIX: pin the intermediate SPKI (GTS WE1), not the leaf.

## Priorities
- **Day-5 CLIENT hardening:** CRIT-1 (bind fire to a revocable server lease — stop trusting `authenticated_`) + MED-3 (intermediate-SPKI pin). Obfuscation only raises cost; the leverage is moving authorization server-side.
- **07-15 SERVER hardening:** CRIT-2 (offline signing / allow-list artifact_url), HIGH-2 (license_key idempotency + increment_uses), HIGH-3 (per-consumer secrets + scoped deliver), HIGH-4 (enforce edge auth fail-closed + lock origin), MED-1 (rate-limit + real replay check), MED-2 (staff binding).

*Note: `backend/lambda_function.py` has pre-existing uncommitted changes — coordinate before editing it.*
