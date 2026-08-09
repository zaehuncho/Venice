#pragma once

#include "OrionExports.h"
#include "UpdateManifest.h"

#include <QtCore/QByteArray>
#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtCore/QUrl>
#include <QtNetwork/QNetworkAccessManager>

namespace orion {

struct LicenseResult {
    bool ok = false;
    QString user;
    QString plan;
    QString message;
    QString token;
    QString error;   // server error code (e.g. "service_disabled","revoked"); empty on a transport failure
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
};

ORION_SECURITY_API VersionResult parseVersionResponse(const QByteArray& payload);
// Parses a successful /api/activate (alias /api/license/redeem) response body,
// including the session-token lease fields (token/tid/expires). Exposed for tests.
ORION_SECURITY_API LicenseResult parseActivateResponse(const QByteArray& payload);
// Parses a /api/license/check heartbeat response body, including
// lease_expires_at (+ the future lease_sig). Exposed for tests.
ORION_SECURITY_API LicenseResult parseLicenseCheckResponse(const QByteArray& payload);
ORION_SECURITY_API int compareSemanticVersions(const QString& lhs, const QString& rhs);

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
