# Server Security Deploy Checklist (Red-Team V2 fixes — 2026-07-17)

These changes are UNCOMMITTED in the working tree (backend/lambda_function.py, the two
webhooks, tests/backend/*). Before deploying, the operator MUST perform the infra actions
below — several fixes fail CLOSED and will break the money path if the params/tables are
missing.

## 1. New / changed SSM parameters

| SSM param | Type | Action | Fix |
|---|---|---|---|
| `/orion/lease_signing_key` | SecureString | **CREATE** — Ed25519 **private** key PEM (below) | NEW-1 |
| `/orion/ed25519_public_key` | **String** (NOT SecureString) | Ensure it is a plain String — the Lambda reads it with `WithDecryption=False`; a SecureString read undecrypted is mangled | CRIT-2 |
| `/orion/ed25519_private_key` | — | **No longer read by the Lambda.** Keep the private key OFFLINE / air-gapped only. Optionally delete from SSM. | CRIT-2 |
| `/orion/edge_auth_secret` | SecureString | **MUST exist** — edge auth now fails CLOSED (absent secret ⇒ every request 403) | HIGH-4 |
| `/orion/webhook_bot_secret` | SecureString | **CREATE** — per-consumer secret for the gumroad + sellhub webhook Lambdas (falls back to `/orion/bot_service_secret` until set) | HIGH-3 |
| `/orion/worker_bot_secret` | SecureString | **CREATE** — per-consumer secret for the Cloudflare worker (optional; accepted if present) | HIGH-3 |
| `/orion/bot_service_secret` | SecureString | Keep — now the Discord-bot-host consumer. Rotate independently of the others. | HIGH-3 |

`require_bot()` accepts ANY one of the three bot secrets, so they can be rotated
independently: a leak of one is fixed by rotating only that param.

## 2. Lease keypair (NEW-1) — GENERATE YOUR OWN (never commit the private key)

**Do NOT commit a private key to the repo.** If the private key is ever public, the lease is
forgeable again and NEW-1 is defeated. Generate the pair on YOUR secure machine:

```
python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K; from cryptography.hazmat.primitives import serialization as s; import base64; k=K.generate(); print(k.private_bytes(s.Encoding.PEM,s.PrivateFormat.PKCS8,s.NoEncryption()).decode()); print('PUB_B64', base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw,s.PublicFormat.Raw)).decode())"
```

- **Private key (PEM)** → set as SSM `/orion/lease_signing_key` (SecureString). Never anywhere else.
- **Public key (`PUB_B64`)** → give it to me and I'll embed it in the client
  `LeaseGate::leaseVerifyPublicKey()` (raw 32 bytes), OR embed it yourself:
  ```cpp
  QByteArray LeaseGate::leaseVerifyPublicKey() {
      return QByteArray::fromBase64("<YOUR_PUB_B64>");
  }
  ```

Once `leaseVerifyPublicKey()` is non-empty, the client REQUIRES a valid `lease_sig` on every
heartbeat. Deploy the server lease-signing FIRST (backward compatible — old clients ignore the
new `lease_sig`/`public_key_id` fields), THEN embed the public key and rebuild the client. Until
then the lease gate stays a no-op (CRIT-1 not yet enforced client-side).

## 3. New DynamoDB tables

| Table | Partition key | Fix |
|---|---|---|
| `orion-ratelimit` | `rl_key` (S) | MED-1 rate limiting (add a TTL attribute on `expires` for auto-cleanup) |
| `orion-update-manifest` | `record_id` (S) | CRIT-2 pre-signed manifest store (create if not already present) |

Rate limiting FAILS OPEN if the table is missing (won't break the money path) but then
does not enforce — create the table to actually enforce the cap.

## 4. Offline update-signing workflow (CRIT-2)

The Lambda no longer signs. To publish an update:
1. `tools/package_orion_release.py` emits `update_manifest.unsigned.json`.
2. On an air-gapped box (holding the update private key), sign the canonical manifest
   bytes (`MANIFEST_SIGN_FIELDS`, `json.dumps(sort_keys=False, separators=(',',':'))`)
   and attach `signature` (base64url, no padding).
3. `POST /api/update` with the signed manifest. The Lambda verifies the signature with the
   PUBLIC key and validates `artifact_url` against the allow-list before storing.

**artifact_url allow-list** (CRIT-2): edit `ARTIFACT_URL_ALLOWLIST` in
`backend/lambda_function.py` to your real release origin(s), or set env
`ORION_ARTIFACT_ALLOWLIST` (comma-separated `host` or `host/path-prefix`). Only HTTPS + an
allow-listed host is accepted.

## 5. Edge auth is now enforced on ALL routes including `/api/bot/*` (HIGH-3 + HIGH-4)

- The `/api/bot/*` edge-auth exemption is REMOVED. The gumroad + sellhub webhook Lambdas
  now attach `X-Edge-Auth` (from `/orion/edge_auth_secret`) to their `/api/bot/provision`
  call — they already read SSM, so just ensure the param is set and the webhook role can
  read it.
- The Cloudflare worker must also inject `X-Edge-Auth: <edge_auth_secret>` on its proxied
  bot calls (it already holds the secret to inject on other routes).
- Optionally ALSO restrict the API Gateway to Cloudflare source IPs (defence in depth).

## 6. Alerting (CRIT-2 / MED-1)

Add a CloudWatch Logs metric filter on the literal token `[ALERT]` (emitted by
`admin_alert()` on `/api/admin/*` + `/api/update` POST) and subscribe SNS/paging. Also
alert on `activate_rate_limited` / `admin_rate_limited` audit rows.

## 7. Not closable in code (needs infra) — see report §"Residual"
- API-Gateway source-IP restriction to Cloudflare.
- SellHub timestamp is still only checked when the header is present and is not part of the
  signed payload; the mint is protected by server-side `order_id` spend-once instead.
- Concurrent-lease cap per license (SECURITY_LEASE_SERVER_CONTRACT §3) — not implemented.
