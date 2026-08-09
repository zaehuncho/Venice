# Orion Backend - Server Handoff Document
**Last Updated:** 2026-06-09  
**Region:** us-east-1

## AWS Resources

### Lambda
- **Function name:** `orion-activate`
- **Runtime:** Python 3.12
- **ARN:** `arn:aws:lambda:us-east-1:987622176566:function:orion-activate`
- **Handler:** `lambda_function.lambda_handler`
- **Backup location:** `~/orion-backup/orion-activate-backup-20260609-055856.zip`
- **New code location:** `~/orion-new/lambda_function.py` (CloudShell)

### API Gateway
- **Type:** HTTP API (v2)
- **ID:** `v348t5hg3i`
- **Endpoint:** `https://v348t5hg3i.execute-api.us-east-1.amazonaws.com`

### DynamoDB Tables

**orion-licenses**
- **Partition key:** `license_key` (String)
- **Attributes:**
  - `license_key` (PK) — e.g. `ORION-XXXX-XXXX-XXXX`
  - `email` — owner email address
  - `plan` — plan tier (standard, pro, etc.)
  - `status` — "active" or "revoked"
  - `revoked` — Boolean
  - `machine_id` — bound machine hash (empty string = unbound)
  - `expiry` — Unix timestamp
  - `created_at` — Unix timestamp
  - `activation_count` — integer
  - `deactivation_count_30d` — integer (rolling 30d window)
  - `deactivation_window_start` — Unix timestamp
  - `last_deactivated_at` — Unix timestamp
  - `discord_user_id` — optional Discord snowflake ID (for ownership/support/beta roles)

**orion-nonces**
- **Partition key:** `nonce` (String)
- **TTL attribute:** `ttl` (600s expiry)
- Used to prevent replay attacks

### SSM Parameter Store (SecureString)
- `/orion/admin_secret` — Admin API authentication secret
- `/orion/token_secret` — HMAC secret for JWT token signing
- **Never log or print these values**

### CloudWatch
- **Log group:** `/aws/lambda/orion-activate`

**Metric Filters (namespace Orion/Security):**
- `orion-activation-failures` → metric: `ActivationFailures`
- `orion-admin-auth-fail` → metric: `AdminAuthFail`
- `orion-device-mismatch` → metric: `DeviceMismatch`
- `orion-deactivate-limit` → metric: `DeactivateLimitHit`
- `orion-lambda-errors` → metric: `LambdaErrors` (namespace Orion/Ops)
- `orion-nonce-replay` → metric: `NonceReplay`

**Alarms (all INSUFFICIENT_DATA = normal for new):**
- `orion-high-activation-failures` — >=10 in 5min
- `orion-admin-brute-force` — >=3 in 5min
- `orion-nonce-replay-burst` — >=5 in 5min
- `orion-device-mismatch-burst` — >=15 in 5min
- `orion-lambda-error-rate` — >=5 in 5min
- **Note:** SNS email alerting has been attached to the alarms.

## Cloudflare
- `api.zaeorion.com` → proxied CNAME → `v348t5hg3i.execute-api.us-east-1.amazonaws.com`
- Current caveat: `GET /api/version` works at the direct API Gateway origin, but
  Cloudflare may challenge the proxied `api.zaeorion.com/api/version` path until
  a WAF/Bot rule skips or allows that route.

## Endpoints

### Owner/Staff route deployment guard

Before claiming the Owner/Staff tools are production-ready, run the local
non-secret contract check against the deployed API:

```bash
python tools/admin/check_backend_contract.py --base-url https://api.zaeorion.com
```

Passing does **not** require valid owner/staff credentials. It verifies that the
route contract exists and fails closed:

- `GET /api/staff/whoami` returns JSON auth failure, not `404`
- `POST /api/staff/login` returns JSON missing-fields failure, not `404`
- `GET /api/admin/staff` returns JSON forbidden/config failure, not `404`
- `POST /api/admin/tamper-report` returns JSON forbidden/config failure, not
  `404`
- `POST /api/staff/tamper-report` returns JSON staff-auth failure, not `404`

If these return `404`, API Gateway or the live Lambda revision has not been
updated to the staff/owner route contract. Do not blindly deploy an older local
`backend/lambda_function.py` over production if production already contains newer
update-manifest routes such as `/api/update`; merge the staff routes into the
current live revision first.

### POST /api/activate
Activates a license key and binds it to a machine.

**Request:**
```json
{
  "license_key": "ORION-XXXX-XXXX-XXXX",
  "machine_id": "<sha256 hash of machine identifiers>",
  "request_nonce": "<random hex string, 16+ bytes>",
  "request_timestamp": 1749456000,
  "discord_user_id": "123456789012345678"  // optional
}
```
**Success (200):**
```json
{"ok": true, "user": "user@example.com", "plan": "standard", "token": "eyJ...", "message": "Activation successful"}
```
**Errors:** `invalid_key`, `revoked`, `expired`, `inactive`, `device_mismatch`, `nonce_replay`, `timestamp_skew`

### GET /api/version
Unauthenticated backend metadata and feature check.

**Success (200):**
```json
{
  "ok": true,
  "api": "orion-backend",
  "version": "0.1.0",
  "env": "production",
  "features": {
    "license_auth": true,
    "deactivate": true,
    "provision": true,
    "admin_license": true,
    "version": true
  }
}
```

### POST /api/deactivate
Unbinds a license from its current machine.

**Request:** Same shape as activate (`license_key`, `machine_id`, `request_nonce`, `request_timestamp`)
**Success:** `{"ok": true, "message": "License deactivated successfully."}`
**Limits:** 3 deactivations per 30 days, 24h cooldown between deactivations.

### POST /api/provision
Creates a new license. **Requires admin secret header.**

**Header:** `X-Orion-Admin-Secret: <secret>`
**Request:**
```json
{
  "email": "user@example.com",
  "plan": "standard",
  "days": 365,
  "discord_user_id": "123456789012345678",  // optional
  "license_key": "ORION-CUSTOM-KEY"          // optional, auto-generated if omitted
}
```
**Success (201):**
```json
{"ok": true, "license_key": "ORION-XXXX-XXXX-XXXX", "email": "...", "plan": "...", "expiry_date": 1783577854, "message": "License provisioned successfully."}
```

### GET /api/admin/license?key=ORION-XXXX-XXXX-XXXX
Lookup a license by key. **Requires admin secret.**

**Response:** Full license record from DynamoDB.

### POST /api/admin/license
Update a license. **Requires admin secret.**

**Actions:**
- `{"license_key": "...", "action": "revoke"}` — Revoke
- `{"license_key": "...", "action": "unrevoke"}` — Unrevoke  
- `{"license_key": "...", "action": "reset_machine"}` — Clear machine binding
- `{"license_key": "...", "email": "new@example.com"}` — Update email
- `{"license_key": "...", "plan": "pro"}` — Update plan
- `{"license_key": "...", "expiry": 1800000000}` — Update expiry
- `{"license_key": "...", "discord_user_id": "123..."}` — Set Discord ID

Updatable fields: `email`, `plan`, `status`, `expiry`, `machine_id`, `discord_user_id`, `deactivation_count_30d`, `last_deactivated_at`, `deactivation_window_start`

### DELETE /api/admin/license?key=ORION-XXXX-XXXX-XXXX
Revokes a license. **Requires admin secret.**

## Admin Workflows

### Provision a new license
```bash
curl -X POST https://api.zaeorion.com/api/provision \
  -H 'Content-Type: application/json' \
  -H 'X-Orion-Admin-Secret: <secret>' \
  -d '{"email":"user@example.com","plan":"standard","days":365}'
```

### Look up a license
```bash
curl 'https://api.zaeorion.com/api/admin/license?key=ORION-XXXX-XXXX-XXXX' \
  -H 'X-Orion-Admin-Secret: <secret>'
```

### Reset machine binding (transfer)
```bash
curl -X POST https://api.zaeorion.com/api/admin/license \
  -H 'Content-Type: application/json' \
  -H 'X-Orion-Admin-Secret: <secret>' \
  -d '{"license_key":"ORION-XXXX-XXXX-XXXX","action":"reset_machine"}'
```

### Update Discord ID
```bash
curl -X POST https://api.zaeorion.com/api/admin/license \
  -H 'Content-Type: application/json' \
  -H 'X-Orion-Admin-Secret: <secret>' \
  -d '{"license_key":"ORION-XXXX-XXXX-XXXX","discord_user_id":"123456789012345678"}'
```

## Test Commands

```bash
# Activate (valid)
TS=$(date +%s); NONCE=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
curl -X POST https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/activate \
  -H 'Content-Type: application/json' \
  -d "{\"license_key\":\"ORION-TEST-2026-0002\",\"machine_id\":\"<machine_hash>\",\"request_nonce\":\"$NONCE\",\"request_timestamp\":$TS}"

# Invalid key
TS=$(date +%s); NONCE=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
curl -X POST https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/activate \
  -H 'Content-Type: application/json' \
  -d "{\"license_key\":\"ORION-FAKE-FAKE-FAKE\",\"machine_id\":\"test\",\"request_nonce\":\"$NONCE\",\"request_timestamp\":$TS}"

# Admin lookup
curl 'https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/admin/license?key=ORION-TEST-2026-0002' \
  -H 'X-Orion-Admin-Secret: <secret>'

# Version
curl https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/version
```

## Security Notes
- All keys use suffix-only logging (`...XXXX`) — full keys never logged
- Admin secret validated via HMAC constant-time compare
- Nonce expiry: 10 minutes
- Timestamp skew limit: 5 minutes
- Deactivation limit: 3 per 30 days, 24h cooldown

## Current Limitations / Production Gaps
1. **No per-ring manifests yet** — `/api/update` is live but serves a single manifest; release-ring support is specified below and pending Lambda work
2. **No CloudFront/download distribution** (not yet deployed)
3. **Single Lambda function** handles all routes - consider splitting if code grows
4. **Backend deploys should now source from this repo** instead of editing CloudShell-only code directly.
5. **Cloudflare blocks direct curl from CloudShell IPs** - use direct APIGW URL for admin testing from CloudShell

## Backup / Recovery
- DynamoDB PITR is enabled for `orion-licenses` with a 35-day recovery window.

## Update Infrastructure (deployed — Ed25519, see docs/UPDATER_CLIENT.md)

The HMAC plan that used to live here was superseded: `GET /api/update` is live and
serves an **Ed25519-signed** manifest (pinned key id `orion-ed25519-v1` embedded in
the client). The wire contract, canonical signing string (8 fields), and client
behavior are specified in `docs/UPDATER_CLIENT.md`; admin publishing flow in
`docs/ADMIN_TOOL.md`. Artifacts are package ZIPs; Cloudflare requires the
`OrionLauncher/<ver> (...)` UA.

## Release Rings — server contract additions (Lambda work, separate session)

The client now sends its ring on every update check and the launcher BLOCKS at
startup until the check resolves (fail-soft on unreachable):

```
GET /api/update?client_version=<semver>&channel=<dev|internal|beta|stable>
```

Required server behavior:

1. **Per-ring manifests.** Keep one signed manifest per channel (e.g. DynamoDB
   `orion-releases` item per ring, or S3 `manifest-<ring>.json`). Respond with the
   manifest for the requested `channel`; missing/unknown `channel` ⇒ treat as
   `beta` (current client default ring; flip the fallback to `stable` at GA).
2. **`channel` response field.** Echo the ring the manifest was served for as a
   top-level `"channel"` field. It is **informational only — NOT part of the
   signed canonical string** (the 8 signed fields are unchanged), so existing
   signatures stay valid. The client logs a mismatch between requested and
   served ring.
3. **Promotion flow.** dev → internal → beta → stable. `stable` only receives
   builds that passed the live + replay gates. Publishing = sign a new manifest
   for that ring; no client change needed.
4. **Pause rollout.** To stop a bad version mid-rollout: re-point the ring's
   manifest at the previous good version. Clients that already updated to the
   bad version see an older `latest_version`; to force them back, also set
   `"allow_rollback": true` + `"mandatory": true` on that manifest (the client's
   downgrade path requires `allow_rollback`, and `mandatory` makes the startup
   gate non-skippable).
5. **Force rollback from a bad version.** Same mechanism as (4); optionally set
   `minimum_supported_version` above the bad version to hard-block it entirely
   (the only state that refuses to run the client).
6. **Mandatory security updates.** `"mandatory": true` ⇒ the client's startup
   gate auto-proceeds with no skip. Clients cannot bypass it (the only
   exception is a dev build-tree launch, which never auto-applies by design).
7. **Updater re-fetch.** OrionUpdater.exe re-fetches the manifest itself; the
   launcher passes the same `channel` query in `--manifest-url`, so both
   requests must resolve to the same ring manifest.
