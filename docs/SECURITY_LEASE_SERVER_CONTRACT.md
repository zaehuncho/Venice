# Fire-Lease Server Contract (CRIT-1) — 07-15 server dependency

Client side of the CRIT-1 fix (docs/SECURITY_REDTEAM.md) shipped 2026-07-09:
`LeaseGate` (native_orion/src/LeaseGate.{h,cpp}) gates `automation_.process()` /
`automationSecurityAllowed()` on a live, server-authorized, revocable lease when
`ORION_LEASE_GATED_FIRE=1`. The flag is **default OFF** until the server work below
lands and is verified against production. Flip the flag on in the launcher
environment (or bake it into the release config) only after every item here ships.

## What the client already does (no server change needed)

- Keeps the `/api/activate` session token client-side (`token`, `tid`, `expires` — it
  was previously discarded) and seeds a lease from a successful activation, capped at
  15 min (`LeaseGate::kFallbackLeaseTtlS`), NOT the token's 30-day expiry.
- Treats the `/api/license/check` heartbeat (5-min timer) as the authoritative lease:
  - `ok:true` + `lease_expires_at` → lease refreshed until `lease_expires_at`
    (falls back to now + 900 s if the field is missing).
  - `ok:false` with an authoritative error code (`revoked`, `expired`,
    `service_disabled`, `device_mismatch`, `invalid_key`, `inactive`) → lease dropped
    immediately (revoke-fast) + session disconnect.
  - Transport failure → lease NOT refreshed; fire fails closed once the lease is
    15 min stale on a **monotonic** clock (system-clock rollback cannot extend it).
- Fails CLOSED: no lease / expired lease / stale lease ⇒ no automation fire, even if
  `authenticated_` is forced true or the auth check is NOPed.

## What the SERVER must provide by 07-15

1. **Signed, short-TTL lease on the heartbeat** (`handle_validate`, POST
   `/api/license/check`):
   - Already returns `lease_expires_at = now + LEASE_TTL_S` (900 s) — keep it.
   - ADD `lease_sig`: a detached **Ed25519** signature (base64url, no padding — same
     convention as the update-manifest signing) over the canonical string:

     ```
     f"{license_key}:{machine_id}:{lease_expires_at}"
     ```

     `license_key` uppercased/trimmed, `machine_id` trimmed, `lease_expires_at` as a
     base-10 integer. Sign with a dedicated lease keypair (SSM, e.g.
     `/orion/lease_signing_key`) — NOT the update-manifest key, NOT the HMAC token
     secret (an HMAC shared secret cannot be shipped in the client; Ed25519 lets the
     client embed only the 32-byte public key).
   - Client hook: paste the base64 public key into `LeaseGate::leaseVerifyPublicKey()`
     (native_orion/src/LeaseGate.cpp). Once non-empty, a valid signature becomes
     MANDATORY for every lease refresh — `LeaseGate::verifyLeaseSignature()` is
     already implemented and tested (`leaseGateSignatureRoundTripAndRejection`).
2. **Revoke-fast:** revoking a license (bot `/revoke`, killswitch, refund flow) must be
   visible on the very next `/api/license/check` — i.e. the handler's existing
   `revoked`/`status`/global-kill checks must stay ahead of any caching. Target: fire
   dies within one heartbeat (≤ 5 min) + lease TTL slack.
   - The handler must return the error **codes** the client kills on (`revoked`,
     `expired`, `service_disabled`, `device_mismatch`, `invalid_key`, `inactive`) in
     the `error` field. Today `handle_validate` returns prose ("license revoked",
     "machine mismatch", "service disabled") which the client treats as fail-soft —
     align the codes or the kill path never triggers.
3. **Concurrent-lease cap (ties into HIGH-1):** count live leases per license
   (token_id × machine_id seen by `/api/license/check` within LEASE_TTL_S) and return
   an authoritative error when the cap is exceeded, so one key can't run N cracked
   clients even with a spoofed `machine_id`.
4. **(Optional, stronger)** have the heartbeat echo + verify the activation `tid`
   (client now sends nothing extra; it keeps `tid` in memory and can POST it) so a
   lease can be bound to a specific activation token record and killed by deleting
   the token row.

## Rollout order

1. Deploy server `lease_sig` + error-code alignment (backward compatible — old
   clients ignore both).
2. Embed the lease public key in `LeaseGate::leaseVerifyPublicKey()`, rebuild.
3. Verify a staging client with `ORION_LEASE_GATED_FIRE=1`: valid lease fires,
   revoke kills fire within one heartbeat, network cut kills fire after 15 min.
4. Default the flag ON for release builds (launcher env or config).
