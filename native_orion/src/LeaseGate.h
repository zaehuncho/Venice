#pragma once

#include "OrionExports.h"

#include <QtCore/QByteArray>
#include <QtCore/QString>

namespace orion {

// CRIT-1 fix (docs/SECURITY_REDTEAM.md): binds the AutomationEngine's ability to
// FIRE to a short-lived, server-authorized, revocable LEASE instead of the
// client-local `authenticated_` bool. A cracked binary that forces
// `authenticated_ = true` (or NOPs the auth check) still cannot fire, because
// fire additionally requires a lease that only a successful, recent
// /api/license/check heartbeat (or the /api/activate grant that seeds it) can
// mint — and the server can revoke it within one heartbeat.
//
// Clock model (anti-rollback): TWO clocks are enforced together.
//   - Wall clock (epoch seconds) against the server-issued `lease_expires_at`.
//   - A MONOTONIC max-staleness window since the last successful heartbeat, so
//     winding the system clock back cannot resurrect an expired lease.
// Both must pass. Fail closed: no lease recorded => no fire.
//
// Rollout flag: ORION_LEASE_GATED_FIRE (env, default OFF). The gate is fully
// built + tested but disabled by default until the 07-15 server work confirms
// the heartbeat lease contract (see docs/SECURITY_LEASE_SERVER_CONTRACT.md).
// When the flag is OFF, fireAllowed() is unconditionally true and the caller's
// behaviour is identical to the pre-lease code path.
class ORION_SECURITY_API LeaseGate final {
public:
    // Server heartbeat cadence is 5 min and the server lease TTL is 900 s
    // (LEASE_TTL_S in backend/lambda_function.py). 15 min of monotonic
    // staleness tolerates two missed heartbeats before failing closed.
    static constexpr qint64 kDefaultMaxStalenessMs = 15 * 60 * 1000;
    // Wall-clock bootstrap TTL used only after successful activation. A
    // heartbeat must carry a future server-issued lease_expires_at; missing or
    // expired heartbeat leases fail closed instead of minting a client TTL.
    static constexpr qint64 kFallbackLeaseTtlS = 900;

    // True iff ORION_LEASE_GATED_FIRE is set to a truthy value (1/true/yes/on).
    // Default OFF until the 07-15 server dependency is confirmed.
    [[nodiscard]] static bool enabledFromEnvironment();

    void setEnabled(bool on) noexcept { enabled_ = on; }
    [[nodiscard]] bool enabled() const noexcept { return enabled_; }

    void setMaxStalenessMs(qint64 ms) noexcept { maxStalenessMs_ = ms > 0 ? ms : kDefaultMaxStalenessMs; }
    [[nodiscard]] qint64 maxStalenessMs() const noexcept { return maxStalenessMs_; }

    // ── Deterministic-clock core (tests drive these directly) ───────────────
    // /api/activate succeeded: seed a lease. The activation token expiry is a
    // 30-day SESSION credential, not a lease, so the seeded lease is capped at
    // kFallbackLeaseTtlS — the heartbeat must keep it alive from then on.
    void recordActivation(qint64 tokenExpiresEpochS, qint64 nowMonotonicMs, qint64 nowEpochS) noexcept;
    // /api/license/check heartbeat returned ok (server says: active, bound to
    // this machine, not revoked, not expired, killswitch off). A missing or
    // non-future lease expiry invalidates the current lease.
    void recordHeartbeatOk(qint64 leaseExpiresAtEpochS, qint64 nowMonotonicMs, qint64 nowEpochS) noexcept;
    // Heartbeat returned an authoritative kill verdict (revoked/expired/...):
    // drop the lease immediately (revoke-fast path).
    void recordHeartbeatKill() noexcept;
    void invalidate() noexcept { recordHeartbeatKill(); }
    [[nodiscard]] bool fireAllowed(qint64 nowMonotonicMs, qint64 nowEpochS) const noexcept;
    [[nodiscard]] QString stateText(qint64 nowMonotonicMs, qint64 nowEpochS) const;

    // ── Real-clock conveniences (app path) ───────────────────────────────────
    void recordActivation(qint64 tokenExpiresEpochS);
    void recordHeartbeatOk(qint64 leaseExpiresAtEpochS);
    [[nodiscard]] bool fireAllowed() const;
    [[nodiscard]] QString stateText() const;

    // ── Lease signature (07-15 server dependency) ────────────────────────────
    // Verifies a detached Ed25519 signature over the canonical lease string
    //   "license_key:machine_id:lease_expires_at"
    // with a 32-byte raw public key (base64url signature, matching the update
    // manifest convention). The private key lives server-side only.
    [[nodiscard]] static bool verifyLeaseSignature(const QByteArray& publicKey,
                                                   const QString& licenseKey,
                                                   const QString& machineId,
                                                   qint64 leaseExpiresAtEpochS,
                                                   const QByteArray& signatureBase64Url);
    // Embedded lease verify key (raw 32 bytes). EMPTY today: the backend does
    // not sign the heartbeat lease yet — that is the documented 07-15 server
    // dependency. While empty, callers skip signature verification and rely on
    // expiry + verdict + max-staleness. Once the server ships a signed lease,
    // embed the public key here and signature verification becomes mandatory.
    [[nodiscard]] static QByteArray leaseVerifyPublicKey();

private:
    bool enabled_ = false;
    bool leaseOk_ = false;
    qint64 leaseValidUntilEpochS_ = 0;
    qint64 lastRefreshMonotonicMs_ = -1;   // -1 == never refreshed (fail closed)
    qint64 maxStalenessMs_ = kDefaultMaxStalenessMs;
};

} // namespace orion
