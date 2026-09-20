#include "LeaseGate.h"

#include "Ed25519.h"

#include <QtCore/QDateTime>
#include <QtCore/QElapsedTimer>

#include <algorithm>

namespace orion {

namespace {

// Process-wide monotonic clock for the real-clock conveniences. QElapsedTimer
// is monotonic on every supported platform (QPC on Windows), so a system
// clock rollback cannot stretch the staleness window.
qint64 monotonicNowMs()
{
    static QElapsedTimer timer;
    if (!timer.isValid()) {
        timer.start();
    }
    return timer.elapsed();
}

qint64 epochNowS()
{
    return QDateTime::currentSecsSinceEpoch();
}

} // namespace

bool LeaseGate::enabledFromEnvironment()
{
#ifdef ORION_PRODUCTION_BUILD
    // A user-controlled environment variable must never disable revocation in a
    // shipping build. Development retains the opt-in switch for offline tests.
    return true;
#else
    const QString v = qEnvironmentVariable("ORION_LEASE_GATED_FIRE").trimmed().toLower();
    return v == QLatin1String("1") || v == QLatin1String("true")
        || v == QLatin1String("yes") || v == QLatin1String("on");
#endif
}

void LeaseGate::recordActivation(qint64 tokenExpiresEpochS, qint64 nowMonotonicMs, qint64 nowEpochS) noexcept
{
    // The activation token expiry (now + 30 d) is a session credential, not a
    // lease. Seed only a heartbeat-sized window so a cracked client cannot
    // replay one activation into a month of offline fire.
    qint64 validUntil = nowEpochS + kFallbackLeaseTtlS;
    if (tokenExpiresEpochS > 0) {
        validUntil = std::min(validUntil, tokenExpiresEpochS);
    }
    leaseOk_ = validUntil > nowEpochS;
    leaseValidUntilEpochS_ = validUntil;
    lastRefreshMonotonicMs_ = nowMonotonicMs;
}

void LeaseGate::recordHeartbeatOk(qint64 leaseExpiresAtEpochS, qint64 nowMonotonicMs, qint64 nowEpochS) noexcept
{
    // A heartbeat is authoritative only while its server-signed expiry is in
    // the future.  Falling back to a client-minted TTL for a missing/expired
    // value lets an attacker replay an old, otherwise valid signed heartbeat
    // every few minutes and keep automation alive forever.  Activation has its
    // own deliberately bounded bootstrap below; heartbeats never get one.
    if (leaseExpiresAtEpochS <= nowEpochS) {
        recordHeartbeatKill();
        return;
    }

    leaseOk_ = true;
    leaseValidUntilEpochS_ = leaseExpiresAtEpochS;
    lastRefreshMonotonicMs_ = nowMonotonicMs;
}

void LeaseGate::recordHeartbeatKill() noexcept
{
    leaseOk_ = false;
    leaseValidUntilEpochS_ = 0;
    lastRefreshMonotonicMs_ = -1;
}

bool LeaseGate::fireAllowed(qint64 nowMonotonicMs, qint64 nowEpochS) const noexcept
{
    if (!enabled_) {
        return true;   // flag OFF: identical to the pre-lease behaviour
    }
    if (!leaseOk_ || lastRefreshMonotonicMs_ < 0) {
        return false;  // fail closed: no lease has ever been granted
    }
    if (nowMonotonicMs - lastRefreshMonotonicMs_ > maxStalenessMs_) {
        return false;  // fail closed: no successful heartbeat recently (monotonic — clock rollback can't help)
    }
    return nowEpochS < leaseValidUntilEpochS_;
}

QString LeaseGate::stateText(qint64 nowMonotonicMs, qint64 nowEpochS) const
{
    if (!enabled_) {
        return QStringLiteral("Lease gate off");
    }
    if (!leaseOk_ || lastRefreshMonotonicMs_ < 0) {
        return QStringLiteral("No lease");
    }
    if (nowMonotonicMs - lastRefreshMonotonicMs_ > maxStalenessMs_) {
        return QStringLiteral("Lease stale");
    }
    if (nowEpochS >= leaseValidUntilEpochS_) {
        return QStringLiteral("Lease expired");
    }
    return QStringLiteral("Lease valid (%1 s left)").arg(leaseValidUntilEpochS_ - nowEpochS);
}

void LeaseGate::recordActivation(qint64 tokenExpiresEpochS)
{
    recordActivation(tokenExpiresEpochS, monotonicNowMs(), epochNowS());
}

void LeaseGate::recordHeartbeatOk(qint64 leaseExpiresAtEpochS)
{
    recordHeartbeatOk(leaseExpiresAtEpochS, monotonicNowMs(), epochNowS());
}

bool LeaseGate::fireAllowed() const
{
    return fireAllowed(monotonicNowMs(), epochNowS());
}

QString LeaseGate::stateText() const
{
    return stateText(monotonicNowMs(), epochNowS());
}

bool LeaseGate::verifyLeaseSignature(const QByteArray& publicKey,
                                     const QString& licenseKey,
                                     const QString& machineId,
                                     qint64 leaseExpiresAtEpochS,
                                     const QByteArray& signatureBase64Url)
{
    if (publicKey.size() != 32 || signatureBase64Url.isEmpty()) {
        return false;
    }
    const QByteArray signature = QByteArray::fromBase64(
        signatureBase64Url, QByteArray::Base64UrlEncoding | QByteArray::OmitTrailingEquals);
    if (signature.size() != 64) {
        return false;
    }
    const QByteArray message = QStringLiteral("%1:%2:%3")
                                   .arg(licenseKey.trimmed().toUpper(), machineId.trimmed())
                                   .arg(leaseExpiresAtEpochS)
                                   .toUtf8();
    return ed25519Verify(publicKey, message, signature);
}

QByteArray LeaseGate::leaseVerifyPublicKey()
{
    // NEW-1 (2026-07-17): the server (backend/lambda_function.py sign_lease) signs the
    // heartbeat lease with Ed25519 over "<license_key>:<machine_id>:<expires>". This is the
    // matching raw 32-byte PUBLIC key (base64). Enforcement is still gated by
    // ORION_LEASE_GATED_FIRE (default OFF) — turning it on requires the server to be DEPLOYED
    // with lease-signing first (see docs/SERVER_DEPLOY_CHECKLIST.md), else valid leases would be
    // rejected. To rotate: regenerate the keypair, set the private half in SSM
    // /orion/lease_signing_key, and replace the base64 below.
    // ROTATED 2026-09-19: the 07-17 private half was never in SSM and could not be found
    // anywhere on this machine or in git history, so the server could not sign a single lease
    // (`[LEASE] signing unavailable: No module named 'cryptography'` + ParameterNotFound), and a
    // production client stopped firing exactly 900 s after unlock. A fresh keypair was generated
    // with tools/signing/new_lease_keypair.py; the private half lives only in SSM
    // /orion/lease_signing_key. Safe to rotate because no production build had shipped yet.
    // To rotate again: regenerate, put the PEM in that SSM parameter, replace the base64 below,
    // and REBUILD (docs/SIGNING_FIX_2026-09-19.md).
    return QByteArray::fromBase64("KKBzDoL7+FrwNmBg86z+Vk84oEdRQFLqfD1ahFb/XhY=");
}

} // namespace orion
