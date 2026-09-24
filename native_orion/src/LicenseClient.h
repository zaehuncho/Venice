#pragma once

#include "OrionExports.h"
#include "UpdateManifest.h"

#include <QtCore/QByteArray>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>
#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtCore/QUrl>
#include <QtNetwork/QNetworkAccessManager>

namespace orion {

// Operator "message of the day" (docs/ADMIN_PANEL_V2_CONTRACT.md §5). Carried as
// `motd {text, level, until}` on /api/license/check and /api/version whenever the
// owner has one set and it has not expired. Empty text == no notice.
struct LicenseMotd {
    QString text;
    QString level = QStringLiteral("info");   // normalised to info | warn | maint
    qint64 untilEpochS = 0;                    // Unix seconds; 0 == no expiry
    [[nodiscard]] bool isEmpty() const noexcept { return text.isEmpty(); }
    // Shown iff there is text and it has not expired (until == 0 or until > now).
    [[nodiscard]] bool activeAt(qint64 nowEpochS) const noexcept
    {
        return !text.isEmpty() && (untilEpochS <= 0 || untilEpochS > nowEpochS);
    }
};

// Customer profile block (backend `profile` on /api/activate and
// /api/license/check). It is a READ-ONLY projection of the license row the
// Profile page renders — Discord identity, plan, expiry, and the HWID-reset
// allowance (the owner's rule: 3 free resets per key, then a paid credit or a
// time deduction).
//
// TOLERANT BY CONSTRUCTION: the live Lambda predates this block, so an absent
// `profile` must leave every field at its zero/empty default and the page must
// still render. `known` is the single "did the server actually send one" flag.
struct LicenseProfile {
    bool known = false;
    QString discordUserId;
    QString discordUsername;
    QString plan;
    // Unix seconds. 0 == LIFETIME (that is how the mint paths store it), never
    // "expired" — callers must branch on lifetime before comparing to now.
    qint64 expiryEpochS = 0;
    qint64 activatedAtEpochS = 0;
    int hwidResetsUsed = 0;
    int hwidResetsFreeTotal = 0;
    int hwidResetsFreeRemaining = 0;
    int hwidPaidCredits = 0;
    [[nodiscard]] bool lifetime() const noexcept { return known && expiryEpochS <= 0; }
};

struct LicenseResult {
    bool ok = false;
    QString user;
    QString plan;
    QString message;
    QString token;
    // Present only when a short-lived Discord pairing code was exchanged.
    // Heartbeats must use this canonical private key, never the consumed code.
    QString canonicalLicenseKey;
    QString error;   // server error code (e.g. "service_disabled","revoked"); empty on a transport failure
    // Admin Panel V2 (§5): `version_blocked` carries the oldest client the server
    // still accepts so the "update required" copy can name it. Empty otherwise.
    QString minClientVersion;
    // Optional operator notice riding the heartbeat (empty text == none).
    LicenseMotd motd;
    // Customer profile block (`profile`). known == false on a backend that does
    // not send one yet; the launcher then keeps whatever it already had.
    LicenseProfile profile;
    // CRIT-1 lease fields. The /api/activate session token is no longer
    // discarded: tokenId + tokenExpiresEpochS identify the server-side token
    // record and seed the fire lease. leaseExpiresAtEpochS comes from the
    // /api/license/check heartbeat (the authoritative lease). leaseSig is the
    // server's detached lease signature — empty until the 07-15 backend work
    // ships it (see docs/SECURITY_LEASE_SERVER_CONTRACT.md).
    QString tokenId;
    qint64 tokenExpiresEpochS = 0;
    qint64 leaseExpiresAtEpochS = 0;
    QByteArray leaseSig;
};

struct VersionResult {
    bool ok = false;
    QString api;
    QString version;
    QString env;
    QString message;
    // Optional operator notice (§5): /api/version carries `motd` like the heartbeat.
    LicenseMotd motd;
};

ORION_SECURITY_API VersionResult parseVersionResponse(const QByteArray& payload);
// Parses a successful /api/activate (alias /api/license/redeem) response body,
// including the session-token lease fields (token/tid/expires). Exposed for tests.
ORION_SECURITY_API LicenseResult parseActivateResponse(const QByteArray& payload);
// Parses a /api/license/check heartbeat response body, including
// lease_expires_at (+ the future lease_sig). Exposed for tests.
ORION_SECURITY_API LicenseResult parseLicenseCheckResponse(const QByteArray& payload);
ORION_SECURITY_API int compareSemanticVersions(const QString& lhs, const QString& rhs);

// Parses the optional `motd` member of a backend response object. Absent, non-object
// or blank-text -> empty. `level` is normalised to info|warn|maint (anything else ->
// info); `until` accepts Unix seconds (number or numeric string) or an ISO-8601
// timestamp, and is 0 when absent. Exposed for tests.
ORION_SECURITY_API LicenseMotd parseMotd(const QJsonValue& value);

// Parses the optional `profile` member of a backend response object. Absent or
// non-object -> a default LicenseProfile with known == false (the CURRENT live
// Lambda sends no profile, and that must not degrade activation). Numeric fields
// accept a JSON number or a numeric string (DynamoDB Decimals serialise either
// way through the Lambda), and every count is clamped at >= 0. Exposed for tests.
ORION_SECURITY_API LicenseProfile parseLicenseProfile(const QJsonValue& value);

// True iff `code` is a server verdict the launcher must kill a running session on
// (and refuse activation for). Exact-match on the CODE: server prose never kills,
// so a transport blip or an unknown string stays fail-soft.
//   revoked | expired | device_mismatch | invalid_key | inactive | service_disabled
//   | frozen | blacklisted | version_blocked | subscription_required
//                                                     (contract §5, revoke-fast)
ORION_SECURITY_API bool isLicenseKillCode(const QString& code);

// [P-H 2026-09-23] Remembered sign-in: does this server verdict mean the stored
// canonical key itself will never work again on this PC (so it must be deleted and the
// customer sent back to AuthGate)? Exact CODE match over BOTH endpoints' spellings:
//   /api/license/redeem : invalid_key | license_revoked | license_expired |
//                         license_invalid_status | frozen | blacklisted | device_mismatch |
//                         device_limit_reached | subscription_required |
//                         discord_signin_required | trial_used | pair_invalid
//   /api/license/check  : invalid_key | revoked | expired | inactive | frozen |
//                         blacklisted | device_mismatch | subscription_required
// Deliberately NOT a refusal (the key is kept; the server still refuses, so nothing
// unlocks): transport failures (empty code), 5xx / internal_error,
// entitlement_unavailable, kill_state_unavailable, config_unavailable, rate_limited,
// timestamp_expired / replay_* (clock or retry), license_record_invalid (backend data),
// version_blocked (update, then the same key works) and service_disabled (owner-wide
// pause: wiping every customer's sign-in on a pause would force every keyless customer
// back through Discord on resume). Unknown codes keep the key.
ORION_SECURITY_API bool isRememberedSignInRefusalCode(const QString& code);

// Copy shown to the user for a failed activate / heartbeat verdict. Distinct text
// for frozen, blacklisted and version_blocked (the latter names min_client_version
// when the server sent it); [CL2-P8-006/008 2026-09-23] timestamp_expired and
// device_mismatch get plain fix-it copy; every other code shows the server's `message` prose,
// falling back to `fallback` when that is empty too.
ORION_SECURITY_API QString licenseErrorUserText(const LicenseResult& result,
                                               const QString& fallback = QString());

// Request bodies for the two license endpoints. Exposed so tests can pin the fields
// both sends carry — `client_version` must ride the /api/license/check heartbeat
// exactly as it rides /api/activate (contract §5 version gate).
ORION_SECURITY_API QJsonObject licenseActivateRequestBody(const QString& licenseKey,
                                                         const QString& machineId,
                                                         const QString& requestNonce,
                                                         qint64 requestTimestamp);
ORION_SECURITY_API QJsonObject licenseCheckRequestBody(const QString& licenseKey,
                                                      const QString& machineId);

class ORION_SECURITY_API LicenseClient final : public QObject {
    Q_OBJECT
public:
    explicit LicenseClient(QObject* parent = nullptr);

    void setServerUrl(const QUrl& url);
    void setPinnedCertificateSha256(QString hex);
    void activate(QString licenseKey, QString machineId);
    // Read-only heartbeat (POST /api/license/check). Used to disable a running
    // session when the server revokes/expires the key or flips the killswitch.
    void validate(QString licenseKey, QString machineId);
    // [CL2-P8-002 round 2 2026-09-23] validate() is a no-op while this is true; the
    // controller queues one re-run instead of losing an event-driven heartbeat.
    [[nodiscard]] bool validationInFlight() const noexcept { return validationInFlight_; }
    void checkVersion();
    // Fetch /api/update and report the parsed client-update manifest. Fail-soft:
    // a network/parse failure emits a manifest with ok == false and never throws.
    // channel = the client's release ring (dev/internal/beta/stable); the server
    // serves the per-ring manifest for it.
    void checkUpdate(QString currentVersion = QString(), QString channel = QString());
    [[nodiscard]] QUrl serverUrl() const { return serverUrl_; }
    // The manifest URL OrionUpdater.exe should re-fetch and re-verify. Carries the
    // same channel query the launcher checked with so both sides see one ring.
    [[nodiscard]] QUrl updateManifestUrl(const QString& channel = QString()) const;

signals:
    void activationFinished(orion::LicenseResult result);
    void validationFinished(orion::LicenseResult result);
    void versionCheckFinished(orion::VersionResult result);
    void updateCheckFinished(orion::UpdateManifest result);

private:
    void postActivate(QString licenseKey, QString machineId);
    void postValidate(QString licenseKey, QString machineId);
    void getVersion();
    void getUpdate(QString currentVersion, QString channel);

    QNetworkAccessManager network_;
    QUrl serverUrl_ = QUrl(QStringLiteral("https://api.zaeorion.com"));
    QString pinnedCertSha256_;
    qint64 lastActivationAttemptMs_ = 0;
    bool activationInFlight_ = false;
    bool validationInFlight_ = false;
    bool versionCheckInFlight_ = false;
    bool updateCheckInFlight_ = false;
};

} // namespace orion

Q_DECLARE_METATYPE(orion::LicenseResult)
Q_DECLARE_METATYPE(orion::VersionResult)
