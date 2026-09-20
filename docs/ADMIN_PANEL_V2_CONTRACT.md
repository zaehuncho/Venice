# Admin Panel V2 — contract (owner-ratified 2026-09-13/14)

Owner picks from the capability list: **audit log + owner alerts (2), kill switch + version gate (3),
key lifecycle (4), fraud signals + chargeback auto-revoke (7), dashboard (8), MOTD (9), owner login
hardening (10)**, and the HWID rule: **three free HWID resets per license, after that the reset
deducts a day from the subscription.** Staff roles and per-staff caps are the foundation the audit
trail stands on, so they ship with it.

> **AMENDED 2026-09-15 (owner).** Two rule changes supersede the text below wherever they disagree:
>
> 1. **Plans.** The only sellable plan is the recurring monthly `month` ($25/mo, a Gumroad
>    *membership* `orion-monthly`), preceded by the free 3-day `trial` and accompanied by a one-time
>    activation fee (a separate Gumroad product `orion-activation` that mints nothing). `lifetime`,
>    `week` and `day` are still ACCEPTED on load, lookup and staff mint — keys already sold must keep
>    resolving — but appear on no customer-facing surface. A recurring membership charge sends
>    `renew: true` to `/api/bot/provision`, which EXTENDS the customer's existing key by 30 days
>    instead of minting a second one.
> 2. **HWID resets.** 3 free per key, then `mode=deduct` with **`deduct_days = 1`** (config key
>    `penalty_days`, default now 1). The **paid credit path is staff-only**: `/api/bot/hwid-credit`
>    and `hwid_paid_credits` survive for goodwill grants, but no customer-facing reply links a store
>    and `reset_policy_defaults.price_url` is gone. A **trial** key gets the same 3 free resets and
>    then `trial_no_deduct` — a trial has no subscription to take a day from and the expiry floor at
>    `now` would otherwise hand it unlimited resets. The 24 h cooldown, the lock, and "staff resets
>    never consume the customer's count" are unchanged.

Ground truth this builds on (verified 2026-09-14 00:xx): the live Lambda `orion-activate`
(acct 987622176566, us-east-1) is byte-identical to `backend/lambda_function.py`; tables
`orion-licenses`, `orion-tokens`, `orion-nonces`, `orion-audit`, `orion-staff`, `orion-staff-audit`,
`orion-config`, `orion-update-manifest`, `orion-ratelimit`, `orion-shards`; test harness
`tests/backend` (moto). Existing client surfaces: `OrionOwner.exe` / `OrionStaff.exe`
(`native_orion/src/AdminToolController.*`, `native_orion/qml/admin/`), `tools/admin/orion_admin.py`,
Discord `discord_launch/orion_worker.js` + `orion_bot.py`, Gumroad webhook
`discord_launch/gumroad_webhook/lambda_function.py`.

Everything below is additive to the live contract unless marked BREAKING. All mutations require a
`reason` string (≤ 200 chars). Timestamps are Unix seconds. Key suffix = last 4 chars for logs.

## 1. Identities and roles

| identity | how it authenticates | role |
|---|---|---|
| owner (break-glass) | `X-Orion-Admin-Secret` (SSM `/orion/admin_secret`) **+ `X-Orion-Admin-TOTP`** when config `owner_totp_required=true` | `owner` |
| owner (daily use) | a staff row with `role=owner`, machine-bound token like any staff | `owner` |
| staff | Bearer token + `X-Machine-Id` (existing `require_staff`) | `admin` or `support` |
| customer via Discord | bot secret + `actor_discord_id` (customer) | — |
| staff via Discord | bot secret + `actor_discord_id` resolved against `orion-staff` | staff's role |
| webhook | existing webhook/bot secrets | `webhook` |

`orion-staff` row (new fields in **bold**): `staff_id, discord_user_id, **role** (owner|admin|support),
disabled, machine_id, token_hash, **enroll_key_hash** (per-staff, one-time, salted), **caps**
`{keys_per_day, resets_per_day, extend_max_days}`, **usage** `{day, keys, resets}`, created_by,
created_at, last_login_at`.

Capability matrix (server-enforced in ONE function `require_capability(actor, cap)`):

| capability | owner | admin | support |
|---|---|---|---|
| license.lookup (key/email/discord/machine) | ✓ | ✓ | ✓ |
| license.create (≤ caps.keys_per_day) | ✓ | ✓ | ✗ |
| license.reset_machine (≤ caps.resets_per_day; policy-aware) | ✓ | ✓ | ✓ |
| license.reset_machine force (ignores policy/cooldown) | ✓ | ✗ | ✗ |
| license.extend (≤ caps.extend_max_days) | ✓ | ✓ | ✗ |
| license.revoke / unrevoke | ✓ | ✓ (alerted to owner) | ✗ |
| license.freeze / unfreeze | ✓ | ✓ | ✗ |
| license.set_plan, license.transfer | ✓ | ✓ (alerted) | ✗ |
| license.set_reset_policy, blacklist add/remove | ✓ | ✗ | ✗ |
| staff.* (create, disable, enable, set_role, set_caps, reset_machine, reissue_enrollment) | ✓ | ✗ | ✗ |
| config.* (kill switch, version gate, MOTD, policy defaults, alert routing) | ✓ | ✗ | ✗ |
| audit.read_all, metrics | ✓ | ✗ | ✗ |
| audit.read_own | ✓ | ✓ | ✓ |

## 2. Endpoints (Lambda router)

All under the existing edge auth. Owner routes keep `/api/admin/*` (owner secret or owner-role staff
token); staff routes `/api/staff/*`. Every handler returns `{ok: bool, error?: code, ...}`.

### Staff management (owner)
`POST /api/admin/staff` body `{action, ...}`:
- `create {discord_user_id, role, caps?}` → `{staff_id, enroll_key}` (enroll_key shown ONCE; only its salted hash is stored). BREAKING: replaces the global `/orion/staff_enroll_key_hash` for new enrolments; existing tokens keep working.
- `disable {staff_id}`, `enable {staff_id}`, `set_role {staff_id, role}`, `set_caps {staff_id, caps}`, `reset_machine {staff_id}` (clears machine binding, revokes tokens), `reissue_enrollment {staff_id}` → new enroll_key, `list` (existing GET stays).
`POST /api/staff/enroll {enroll_key, staff_id, machine_id, nonce, timestamp}` unchanged shape, but validates against the staff row's own hash.

### License operations
`POST /api/admin/license` (owner) and `POST /api/staff/license` (staff, capability-gated) — same body
`{action, reason, key? | discord_user_id? | email? | machine_id?, ...}`:
- `lookup` → license row (machine_id masked to last 6 for support), `reset_history`, `flags`.
- `create {plan, days, count=1, discord_user_id?, email?, note?}` → keys (count ≤ 25 per call; admin bounded by caps). Fixes `handle_provision` (today it mints rows without plan/expiry).
- `revoke {key}` / `unrevoke {key}`.
- `reset_machine {key, force?}` — see §3.
- `extend {key, days}`; `set_plan {key, plan}`; `freeze {key}` / `unfreeze {key}` (status `frozen`; the clock stops: `frozen_at` recorded, on unfreeze `expiry += now - frozen_at`; heartbeat returns `frozen`).
- `transfer {key, discord_user_id?, email?}` (clears machine binding, resets free-reset counter? NO — counters travel with the key).
- `set_reset_policy {key, free_resets?, penalty_days?, locked?}` (owner).
- `blacklist {machine_id? | discord_user_id?}` / `unblacklist` (owner) → rows `BLACKLIST#machine#<id>`, `BLACKLIST#discord#<id>` in `orion-licenses`; activate/trial/heartbeat refuse with `blacklisted`.
Discord/email lookups need a `discord_user_id` GSI on `orion-licenses` (create it; fall back to scan only when the GSI is absent, and say so in the response `lookup_mode`).

### HWID reset policy (§3), audit (§4), config (§5), metrics (§6) endpoints:
- `GET /api/admin/audit?since&until&actor&action&target&cursor&limit≤200` (owner); `GET /api/staff/audit?cursor` (own rows only).
- `GET/POST /api/admin/config` — read/update: `global_kill`, `min_client_version`, `blocked_versions[]`, `motd {text, level(info|warn|maint), until}`, `reset_policy_defaults {free_resets:3, penalty_days:1, cooldown_s:86400, self_service:true}` (no `price_url` as of 2026-09-15), `fraud_thresholds`, `owner_totp_required`, `owner_ip_allowlist[]`, `alerts {owner_discord_user_id, events[]}`.
- `GET /api/admin/metrics`.
- BREAKING: `/api/bot/killswitch` loses its write actions (status read only). Global kill is owner-only.

## 3. HWID reset policy (the owner's rule)

License fields: `hwid_free_resets` (default config 3), `hwid_resets_used`, `hwid_paid_credits`,
`hwid_reset_locked`, `reset_history[]` (last 20: `{ts, by(actor_type:id), mode, machine_before_suffix}`),
`last_reset_at`.

Self-service (`/api/bot/hwid-reset` from either Discord front-end):
1. cooldown `reset_policy.cooldown_s` since `last_reset_at` (24 h) → `cooldown` with `retry_at`.
2. `hwid_reset_locked` → `locked` (tell the customer to open a ticket).
3. if `hwid_resets_used < hwid_free_resets` → reset, `used += 1`, mode `free`.
4. else if `hwid_paid_credits > 0` → reset, `credits -= 1`, mode `paid` (staff goodwill only).
5. else, if `plan == "trial"` → `trial_no_deduct` (both with and without `mode`): a trial has no
   subscription to deduct from, and nothing is unbound.
6. else the request must carry `mode: "deduct"` (the bot asks the customer to confirm): deduct
   `penalty_days` (= `deduct_days`, default **1**) from `expiry`; refuse `insufficient_time` if
   `expiry - now < penalty_days`; a key with no expiry (`expiry == 0`, staff lifetime comp) cannot
   deduct → `paid_only` ("open a ticket"). Mode `deduct` is recorded; the bot reply shows the new
   expiry. Without `mode` the response is `payment_required {confirm_required: true, penalty_days,
   deduct_days}` — the CODE keeps its old name so both deployed front-ends keep working, but nothing
   is for sale and no `price_url` is sent.
Staff credit path: `/api/bot/hwid-credit` grants `hwid_paid_credits += 1` on the customer's newest
active key (by `discord_user_id`), audited. The customer-facing Gumroad "HWID reset" product is
RETIRED (2026-09-15); the webhook handler stays only so an in-flight sale still lands.
Staff resets: never consume the customer's free count, capped by `caps.resets_per_day`, audited with
the staff identity; owner `force` bypasses cooldown/lock.

## 4. Audit (single table, actor on every row)

`orion-audit` v2 row: `{audit_id (ulid), day (YYYY-MM-DD, partition for the GSI), ts, actor_type
(owner|staff|customer|webhook|system), actor_id, role, action, target_type (license|staff|config|global),
target (key suffix / staff_id / config key), reason, result (ok|error code), ip, details{}}`.
GSI `day-ts-index` (`day` HASH, `ts` RANGE) for paged reads. Existing `audit_log()` and `staff_audit()`
call sites route through one `audit(actor, action, target, reason, result, details)`.
BREAKING for Discord: both front-ends MUST forward `actor_discord_id`; `/api/bot/deliver` refuses
without it and resolves the actor against `orion-staff` (role admin+), replacing the ADMINISTRATOR-bit check.
Owner alerts: a Discord DM (bot token in SSM) to `alerts.owner_discord_user_id` for events in
`alerts.events` (defaults: staff.create, staff.disable, license.revoke, license.create when count>5 or
by admin, license.reset_machine when a staff member exceeds 5/day, config.*, blacklist.*, fraud.flag,
webhook.chargeback). Alerts never block the operation; failures are audited as `alert_failed`.

## 5. Kill switch, version gate, MOTD, owner hardening

- `global_kill` stays; only owner routes may set it.
- Version gate: `/api/version`, `/api/activate`, `/api/license/check` compare the client's
  `client_version` (already sent on activate; add to check) against `min_client_version` and
  `blocked_versions` → error code `version_blocked` with `min_client_version`. Launcher: treat
  `version_blocked` like `service_disabled` but show "update required".
- MOTD: `/api/version` and `/api/license/check` include `motd` when set and `until > now`. Launcher shows
  a dismissable banner (level colours info/warn/maint) on the Remote Play page top-centre slot.
- Owner TOTP: SSM `/orion/owner_totp_secret` (base32); when `owner_totp_required` is true every
  admin-secret request needs a valid 30 s TOTP (RFC 6238, ±1 step). The Qt owner tool prompts for
  it at login and caches it for the session. Default OFF until the owner enrolls (endpoint
  `POST /api/admin/config {action: totp_enroll}` returns the otpauth URI once; `totp_confirm {code}` turns
  the requirement on). Rotation: `POST /api/admin/config {action: rotate_admin_secret}` writes a new
  secret to SSM and returns it once.
- IP allowlist: `owner_ip_allowlist` (CIDRs); when non-empty, `/api/admin/*` from other IPs → 403
  `ip_not_allowed` (audited).
- Revoke-fast (BREAKING fix): `handle_validate` returns the six error CODES the launcher already
  kills on (`revoked|expired|device_mismatch|invalid_key|inactive|service_disabled`) plus `frozen`,
  `blacklisted`, `version_blocked`; prose moves to a `message` field.

## 6. Fraud signals + dashboard

- On activate: `machine_history[]` (last 10 distinct machine_id suffixes with ts). If distinct
  machines in 30 d > `fraud_thresholds.machines_30d` (default 3) or resets in 30 d >
  `fraud_thresholds.resets_30d` (default 4) → `flags.suspect=true`, audit `fraud.flag`, owner alert;
  the key keeps working unless the owner acts (no auto-kill).
- Gumroad webhook: `refunded` / `disputed` / `chargebacked` → `status=revoked`, `revoke_reason`, audit
  `webhook.chargeback`, owner alert. (Refund already revokes; add dispute/chargeback + alert.)
- Heartbeat records `last_check_at`, `client_version` on the license row.
- `GET /api/admin/metrics` → `{licenses: {active, frozen, revoked, expired, by_plan{}}, trials:
  {active, claimed_7d, converted_30d}, activations: {24h, 7d}, online_now (last_check_at ≥ now-900),
  resets: {24h, 7d, paid_7d, deduct_7d}, versions{}, staff: {active, actions_24h}, fraud_flagged}`.
  Computed by scan on request (owner-only, rate-limited 6/min); good enough below ~50k rows.

## 7. Client surfaces

- **OrionOwner.exe** (primary owner panel): login (secret + TOTP), Dashboard (metrics), Licenses
  (lookup/create/revoke/extend/freeze/transfer/reset with policy view/blacklist), Staff (create →
  shows the one-time enroll key, disable/enable, role, caps, usage today), Audit (paged, filters),
  Config (kill switch, version gate, MOTD, policy defaults, alerts, TOTP enrol, secret rotation).
- **OrionStaff.exe**: login (enroll/login as today, sends `X-Machine-Id`), Licenses limited by role,
  own audit, own caps/usage.
- **CLI** `tools/admin/orion_admin.py`: field names aligned to the Lambda; new subcommands mirror
  the actions above; `ROLE_CAPABILITIES` becomes display-only.
- **Discord**: `/hwid_reset` gains the confirm step for `deduct`; `/deliver` forwards the actor and is
  refused for non-staff; a `/motd` (owner) is optional. `/purchase` is ONE ephemeral embed + ONE link
  button to the Worker's `STORE_URL` — no tiers, no prices (2026-09-15).
- **Launcher**: `version_blocked` handling, MOTD banner, `frozen`/`blacklisted` messages.

## 8. Tests and rollout

- `tests/backend`: capability matrix per role, staff create→enroll→login→act, reset policy state
  machine (free×3→staff credit→deduct 1 day→trial_no_deduct/insufficient→locked→force), the plan
  surface + the monthly renewal (`tests/backend/test_plan_surface.py`), audit rows on every mutation with actor,
  version gate, MOTD, TOTP on/off, IP allowlist, blacklist, fraud flag, metrics shape, revoke-fast
  codes, `/api/bot/deliver` actor requirement, `/api/bot/killswitch` write removal.
- Rollout: deploy the Lambda (additive first, then the two BREAKING Discord changes together with the
  bot/worker deploy), create the `discord_user_id` GSI and the audit GSI, set config defaults, enrol the
  owner's TOTP, then ship the tools. The owner deploys or says "deploy" — nothing is pushed by default.
