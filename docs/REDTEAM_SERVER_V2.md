# Orion Licensing Server — Red-Team V2 (post-rewrite verification)

Read-only adversarial review of the rewritten `backend/lambda_function.py` (commit `293bbe56`)
against the 07-15 server fixes in `docs/SECURITY_REDTEAM.md`, plus the two webhooks that call it:
`discord_launch/gumroad_webhook/lambda_function.py` and
`discord_launch/deployed_bundle/orion-sellhub-webhook/lambda_v6.py`.

**Headline: NONE of the six 07-15 server fixes actually landed.** The rewrite added staff/lease
plumbing and cleaned up some handlers, but every original server-side hole (CRIT-2, HIGH-2,
HIGH-3, HIGH-4, MED-1, MED-2) is still open, and the rewrite introduced a few new issues. The
single most exploitable path remains: **`/api/bot/deliver` mints lifetime keys behind one shared
secret and is exempt from (already fail-open) edge auth.**

---

## Per-original-fix verdicts

### CRIT-2 — /api/update signing oracle → **STILL-OPEN (HARD)**
`handle_post_update` (`backend/lambda_function.py:404-477`) is unchanged in substance: it loads the
Ed25519 **private key** into the Lambda (`load_ed25519_private_key`, `:56-62`, key in SSM
`/orion/ed25519_private_key`) and signs **attacker-supplied** `artifact_url` + `sha256` verbatim
(`manifest_dict` built from `body[...]`, `:424-436`). There is **no offline signing**, **no
`artifact_url` allow-list**, **no host/HTTPS validation**, and **no rate-limit/lockout** on
`/api/update` or `/api/admin/*`. The Lambda still *is* the oracle.
- **Exploit:** leak of `/orion/admin_secret` (one static SSM value, `require_admin` `:104-111`)
  → `POST /api/update {artifact_url:"https://evil/x.zip", sha256:"<hash of trojan>", ...}` → server
  returns a valid Ed25519 signature → every client's `UpdateManifest` verifies it → auto-applied RCE
  on all installs.
- **Fix:** move signing off the Lambda (sign `update_manifest.unsigned.json` on an air-gapped box,
  Lambda only stores the pre-signed blob); if online signing must stay, hard allow-list
  `artifact_url` host + require it be an owned HTTPS origin; add per-operator admin creds + throttle
  + alerting on `/api/admin/*` and `/api/update` POST.

### HIGH-2 — webhook valid-key replay → **STILL-OPEN on Gumroad / PARTIAL on SellHub**
The prescribed fix (idempotency keyed on `license_key`, `increment_uses_count="true"`, reject
`uses>1`) is implemented in **neither** webhook.
- **Gumroad** (`gumroad_webhook/lambda_function.py`): `verify_license_key` still sends
  `increment_uses_count="false"` (`:91`) so the same key verifies unlimited times; idempotency is
  keyed on `event_key = f"{sale_id}:{...}"` (`:208`) — attacker-controlled `sale_id`, **not**
  `license_key`; no `license_key` dedupe anywhere. **Exploit unchanged:** buy the cheapest tier once,
  then with the Ping URL token POST forged pings (fresh `sale_id`, the constant `seller_id`
  `GUMROAD_SELLER_ID`, the genuine `license_key`) → each mints a fresh backend license + DMs it.
  One $ → unlimited keys of that tier. **STILL-OPEN.**
- **SellHub** (`lambda_v6.py`): mitigated *incidentally*, not by the prescribed fix. Requests need a
  valid HMAC (`verify_signature`, `:60-62`) over the body, and mint is gated by `order_id`
  idempotency (`:242-244`) — so an attacker can't forge a new `order_id` and a byte-exact replay is
  deduped. It is **still not** keyed on `license_key`, and timestamp replay is only checked when the
  header is present (`:157`) and is not part of the signed payload — but `order_id` dedupe holds the
  line. **PARTIAL.**

Note: even if a webhook dedupes, `/api/bot/provision` itself (`:1232-1262`) does **not** enforce
`order_id` uniqueness (stores it, never conditions on it) — see HIGH-3.

### HIGH-3 — shared bot secret + bot routes off edge auth → **STILL-OPEN (HARD)**
- One shared secret: `require_bot` (`:113-120`) checks a single SSM value
  `/orion/bot_service_secret`. Both webhooks send that same secret (`provision_license`,
  gumroad `:150` / sellhub `:125`) and the Cloudflare worker holds it too. **No per-consumer
  secrets.** One leak = full mint.
- Bot routes are **exempt from edge auth**: router `if not path.startswith("/api/bot/")`
  (`:1272`) → `/api/bot/*` never calls `require_edge_auth`. The execute-api origin is public +
  hardcoded in both webhooks (`PROVISION_URL`), so bot routes are reachable directly.
- **No order scoping.** `handle_bot_deliver` (`:1206-1230`) mints any plan incl. `lifetime`
  (`expiry=0`) with **no `discord_id`, no `order_id`** required. `handle_bot_provision`
  (`:1232-1262`) never dedupes `order_id`.
- **Exploit:** any leak of the shared bot secret → `POST /api/bot/deliver {"plan":"lifetime"}`
  repeatedly → unlimited free lifetime keys, bypassing Cloudflare entirely.
- **Fix:** per-consumer secrets (webhook/bot/worker) rotated independently; require + condition
  `order_id` uniqueness on provision/deliver; put `/api/bot/*` behind edge auth or an API-GW
  resource policy restricting source IPs to Cloudflare.

### HIGH-4 — edge auth fail-open → **STILL-OPEN (HARD)**
`require_edge_auth` (`:1037-1053`) still fails open two ways:
1. **Secret absent → allow.** `if not secret: ... return None` (`:1042-1045`) — a missing/misnamed
   SSM param `/orion/edge_auth_secret` silently disables the gate.
2. **Enforcement is opt-in.** Mismatch only blocks when `EDGE_AUTH_ENFORCE` env var is truthy
   (`:1040`, `:1051`); default (unset) is log-only → allow. So it is *not* enforced by default and
   the original "fails open" verdict stands verbatim.
Combined with HIGH-3's `/api/bot/*` exemption, even a fully-enforced edge auth would not protect the
mint endpoints. **Fix:** fail **closed** when the secret is absent; drop the `EDGE_AUTH_ENFORCE`
gate (enforce always); restrict API-GW to Cloudflare source IPs / require a CF-injected header.

### MED-1 — activate rate-limit + nonce replay → **STILL-OPEN (activate) / PARTIAL (staff)**
`handle_activate` (`:184-337`) reads only `license_key` + `machine_id`; it **never reads a nonce or
timestamp** — the activate replay fields remain dead. There is **no server-side rate-limit** on
activate / redeem / validate / verify / admin / bot anywhere in the Lambda. The client's 1250 ms
guard is irrelevant to a direct caller. *Partial credit:* the new **staff** endpoints do enforce a
real nonce + timestamp (`check_nonce` conditional-put `:769-780`, ts skew `:801-806`) — but that was
not the MED-1 target. **STILL-OPEN for the money endpoints.**

### MED-2 — staff machine-binding unconditional → **STILL-OPEN**
`require_staff` still binds only when the header is present: `if machine_id and ti.get("machine_id")
and ti["machine_id"] != machine_id` (`:740`). A stolen staff bearer token used **without** the
`X-Machine-Id` header → `machine_id == ""` → the whole clause short-circuits → binding skipped.
Exactly the original finding. (`handle_staff_login`/`enroll` require `machine_id` in the *body*, but
token *use* does not.) **Fix:** require the header on every `require_staff` call and compare
unconditionally (reject when the token has a bound machine and the header is missing or differs).

---

## New findings introduced / surfaced by the rewrite (ranked)

### NEW-1 — HIGH (SOFT): heartbeat lease is UNSIGNED → CRIT-1 mitigation is defeatable
`handle_validate` (`/api/license/check`, `:1055-1094`) returns a **plaintext** `lease_expires_at`
with no signature, and requires **no session token** — only `license_key` + matching `machine_id`.
The client is built to require a signed lease: `LeaseGate::verifyLeaseSignature` exists
(`native_orion/src/LeaseGate.cpp:124-143`) but `leaseVerifyPublicKey()` returns an **empty key**
(`:145-152`) with the comment *"07-15 SERVER DEPENDENCY: once backend/lambda_function.py signs the
heartbeat lease with Ed25519 … the client will REQUIRE a valid signature"*. That server half was
never delivered, so `recordHeartbeatOk` (`:54-64`) trusts whatever `lease_expires_at` comes back.
- **Exploit:** a MITM (edge auth is fail-open, HIGH-4) or a cracked/proxied client returns
  `{"ok":true,"lease_expires_at": <far future>}` → `fireAllowed()` passes indefinitely. The lease
  gate — the entire CRIT-1 server-side control — provides no cryptographic assurance.
- **Fix:** sign the lease per `docs/SECURITY_LEASE_SERVER_CONTRACT.md`
  (`Ed25519(license_key:machine_id:lease_expires_at)`), return the signature + `public_key_id`, and
  ship the public key in `leaseVerifyPublicKey()`. Also require a valid session token on
  `/api/license/check` so an unauthenticated caller can't harvest leases from a known key+machine.

### NEW-2 — MED (SOFT): device-cap TOCTOU race in `handle_activate`
Binding + count are read-modify-write, not atomic: reads `activations`/`bound_machine`
(`:258-260`), gates on `activations >= max_devices` (`:262-266`), then `SET activations = :a`
with a **client-computed** `activations+1` (`:313-323`) — no `ConditionExpression`.
- **Exploit:** two concurrent `POST /api/activate` from two different machines on a fresh
  `max_devices=1` key both read `activations=0` / `bound_machine=""`, both pass, both write → two
  machines activated, counter lands at 1 (last-writer-wins). Repeatable to fan out one license past
  its device cap. Same "two requests both win" class the brief flags.
- **Fix:** atomic `ADD activations :one` plus a `ConditionExpression` that asserts
  `bound_machine` is empty/equal AND `activations < max_devices`; reject on
  `ConditionalCheckFailedException`.

### NEW-3 — MED (SOFT): Gumroad URL token compared with `!=` (timing-unsafe)
`gumroad_webhook/lambda_function.py:174` — `token != get_secret("/orion/gumroad_webhook_token")`
compares the URL path secret with Python `!=` (short-circuits on first differing byte) instead of
`hmac.compare_digest`. A remote timing oracle on the webhook's only auth factor. (Network jitter
raises the bar, but it's a needless side channel on the secret that gates HIGH-2's exploit.)
- **Fix:** `hmac.compare_digest(token, expected)`.

### NEW-4 — LOW (SOFT): SellHub `verify_signature` uses `lstrip("sha256=")` — strips hex chars
`lambda_v6.py:62` — `sig_header.lower().lstrip("sha256=")` treats the argument as a **character
set**, not a prefix, so it also eats leading digest chars in `{2,5,6,a}`. For any signature whose
hex starts with those, the compare fails → legitimate SellHub webhooks intermittently rejected
(fails **closed**, so not a bypass — a delivery-reliability bug). **Fix:**
`sig_header[7:] if sig_header.lower().startswith("sha256=") else sig_header`.

### NEW-5 — LOW: `handle_validate` has an unguarded `json.loads`
`:1058` — `json.loads(event.get("body") or "{}")` with no try/except (every other handler wraps it).
Malformed body → unhandled exception → 500 (and potential stack noise in logs). Trivial DoS / noise.
**Fix:** wrap and return `err("invalid_json")`.

### NEW-6 — LOW: webhook error responses echo internal detail
Both webhooks return `{"error":"revoke_failed","detail":str(e)}` / `"provision_failed"` (gumroad
`:242,259`; sellhub `:234,271`). Leaks internal exception text (table names, boto errors) to the
caller. **Fix:** log server-side, return a generic error body.

---

## Stress-tested negatives (things that are actually OK)
- **Token/hash compares** in `handle_deactivate`/`handle_verify`/`require_admin`/`require_bot`/staff
  enroll use `secrets.compare_digest` / `hmac.compare_digest` — timing-safe. (The two exceptions are
  NEW-3 and NEW-4 above.)
- **Trial + trial-machine claims** (`:272-282`, `handle_bot_trial :1189-1194`) use conditional
  `put_item(attribute_not_exists)` — genuinely atomic, no double-claim race. Marker keys are
  `TRIAL#`/`TRIALMACHINE#`-prefixed so they can't collide with real `XXXX-XXXX` keys.
- **DynamoDB expressions** are fully parameterized (`ExpressionAttributeValues`/`Names`) — no
  injection surface; `admin_search`'s `q` is used only as a key value.
- **SellHub replay** is not a free-mint despite the timestamp/lstrip weaknesses, because a forged
  `order_id` needs the HMAC secret and byte-exact replays hit `order_id` idempotency.
- **No secret is returned or logged** by the main Lambda (edge-auth logs UA only; minted `license_key`
  in responses is the caller's intended payload).

---

## Priority to actually close
1. **HIGH-3 + HIGH-4** together (bot mint reachable behind one fail-open/exempt gate) — highest
   real-world exploitability; needs no client leak, only the shared bot secret.
2. **CRIT-2** — offline signing / `artifact_url` allow-list (worst blast radius: RCE-on-all).
3. **NEW-1** — sign the lease (finishes CRIT-1; client is already waiting on it).
4. **HIGH-2 (Gumroad)** — `license_key` idempotency + `increment_uses_count="true"`.
5. **NEW-2 / MED-1 / MED-2** — atomic activate, activate rate-limit + nonce, unconditional staff
   binding.
