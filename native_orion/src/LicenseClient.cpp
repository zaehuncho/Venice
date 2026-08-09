#include "LicenseClient.h"

#include "NetworkSecurity.h"

#include <QtCore/QDateTime>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QStringList>
#include <QtCore/QUrlQuery>
#include <QtCore/QUuid>
#include <QtCore/QVector>
#include <QtNetwork/QNetworkReply>
#include <QtNetwork/QNetworkRequest>
#include <QtNetwork/QSslCertificate>
#include <QtNetwork/QSslConfiguration>

#include <algorithm>
#include <ctime>

namespace orion {

namespace {

QVector<int> versionParts(const QString& value)
{
    QVector<int> parts;
    const auto split = value.trimmed().split(QLatin1Char('.'));
    parts.reserve(split.size());
    for (const QString& rawPart : split) {
        int digitCount = 0;
        while (digitCount < rawPart.size() && rawPart.at(digitCount).isDigit()) {
            ++digitCount;
        }
        parts.push_back(digitCount > 0 ? rawPart.left(digitCount).toInt() : 0);
    }
    while (parts.size() < 3) {
        parts.push_back(0);
    }
    return parts;
}

} // namespace

VersionResult parseVersionResponse(const QByteArray& payload)
{
    VersionResult result;
    QJsonParseError parseError;
    const auto doc = QJsonDocument::fromJson(payload, &parseError);
    if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
        result.message = QStringLiteral("Invalid backend version response.");
        return result;
    }

    const auto obj = doc.object();
    result.api = obj.value(QStringLiteral("api")).toString();
    if (result.api.isEmpty()) {
        // Backend >= 0.4.0 identifies via "service" (Lambda function name) instead
        // of the documented "api" field; accept both so a backend redeploy can't
        // flip the Patch pill to "API offline" while the server is healthy.
        result.api = obj.value(QStringLiteral("service")).toString();
    }
    result.version = obj.value(QStringLiteral("version")).toString();
    result.env = obj.value(QStringLiteral("env")).toString();
    if (!obj.value(QStringLiteral("ok")).toBool(false)) {
        result.message = obj.value(QStringLiteral("message")).toString(
            obj.value(QStringLiteral("error")).toString(QStringLiteral("Backend version check failed.")));
        return result;
    }
    if (result.api != QLatin1String("orion-backend") && result.api != QLatin1String("orion-activate")) {
        result.message = QStringLiteral("Unexpected backend API identity.");
        return result;
    }
    if (result.version.trimmed().isEmpty()) {
        result.message = QStringLiteral("Backend version is missing.");
        return result;
    }

    result.ok = true;
    result.message = QStringLiteral("Backend version available.");
    return result;
}

LicenseResult parseActivateResponse(const QByteArray& payload)
{
    LicenseResult result;
    const auto obj = QJsonDocument::fromJson(payload).object();
    result.ok = obj.value(QStringLiteral("ok")).toBool(false) || obj.value(QStringLiteral("success")).toBool(false);
    result.user = obj.value(QStringLiteral("user")).toString(obj.value(QStringLiteral("username")).toString());
    result.plan = obj.value(QStringLiteral("plan")).toString();
    result.token = obj.value(QStringLiteral("token")).toString(obj.value(QStringLiteral("access_token")).toString());
    // CRIT-1: keep the signed session token's identity CLIENT-SIDE (it used to
    // be discarded). The backend returns "tid" (handle_activate); accept the
    // documented "token_id" spelling too.
    result.tokenId = obj.value(QStringLiteral("tid")).toString(obj.value(QStringLiteral("token_id")).toString());
    result.tokenExpiresEpochS = static_cast<qint64>(obj.value(QStringLiteral("expires")).toDouble(0.0));
    result.leaseSig = obj.value(QStringLiteral("sig")).toString().toUtf8();
    result.error = obj.value(QStringLiteral("error")).toString();
    result.message = obj.value(QStringLiteral("message")).toString(
        obj.value(QStringLiteral("error")).toString(result.ok ? QStringLiteral("License activated.") : QStringLiteral("Activation failed.")));
    return result;
}

LicenseResult parseLicenseCheckResponse(const QByteArray& payload)
{
    LicenseResult result;
    const auto obj = QJsonDocument::fromJson(payload).object();
    // A structured ok:false with an error code is authoritative (kill). A transport
    // failure (timeout / pin / empty body) leaves error empty so the caller fail-soft:
    // a network blip must NEVER lock out a paying user mid-session.
    result.error = obj.value(QStringLiteral("error")).toString();
    result.message = obj.value(QStringLiteral("message")).toString(result.error);
    result.ok = obj.value(QStringLiteral("ok")).toBool(false);
    result.plan = obj.value(QStringLiteral("plan")).toString();
    // CRIT-1: the heartbeat is the authoritative fire lease. lease_expires_at is
    // now + LEASE_TTL_S server-side; lease_sig is the 07-15 signed-lease field.
    result.leaseExpiresAtEpochS = static_cast<qint64>(obj.value(QStringLiteral("lease_expires_at")).toDouble(0.0));
    result.leaseSig = obj.value(QStringLiteral("lease_sig")).toString().toUtf8();
    return result;
}

int compareSemanticVersions(const QString& lhs, const QString& rhs)
{
    const QVector<int> left = versionParts(lhs);
    const QVector<int> right = versionParts(rhs);
    const int count = std::max(left.size(), right.size());
    for (int i = 0; i < count; ++i) {
        const int a = i < left.size() ? left.at(i) : 0;
        const int b = i < right.size() ? right.at(i) : 0;
        if (a < b) return -1;
        if (a > b) return 1;
    }
    return 0;
}

LicenseClient::LicenseClient(QObject* parent)
    : QObject(parent)
{
    serverUrl_ = QUrl(productionApiBaseUrl());
    setPinnedCertificateSha256(productionApiPinnedCertificateSha256());
}

void LicenseClient::setServerUrl(const QUrl& url)
{
    if (url.isValid() && url.scheme() == QLatin1String("https")) {
        serverUrl_ = url;
    }
}

void LicenseClient::setPinnedCertificateSha256(QString hex)
{
    pinnedCertSha256_ = hex.trimmed().toLower();
}

void LicenseClient::activate(QString licenseKey, QString machineId)
{
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    if (activationInFlight_ || (lastActivationAttemptMs_ > 0 && (now - lastActivationAttemptMs_) < 1250)) {
        LicenseResult result;
        result.message = QStringLiteral("Activation is rate limited locally. Wait a moment and retry.");
        emit activationFinished(result);
        return;
    }
    activationInFlight_ = true;
    lastActivationAttemptMs_ = now;
    postActivate(std::move(licenseKey), std::move(machineId));
}

void LicenseClient::validate(QString licenseKey, QString machineId)
{
    if (validationInFlight_) {
        return;
    }
    validationInFlight_ = true;
    postValidate(std::move(licenseKey), std::move(machineId));
}

void LicenseClient::checkVersion()
{
    if (versionCheckInFlight_) {
        return;
    }
    versionCheckInFlight_ = true;
    getVersion();
}

void LicenseClient::checkUpdate(QString currentVersion, QString channel)
{
    if (updateCheckInFlight_) {
        return;
    }
    updateCheckInFlight_ = true;
    getUpdate(std::move(currentVersion), std::move(channel));
}

QUrl LicenseClient::updateManifestUrl(const QString& channel) const
{
    QUrl url = serverUrl_;
    url.setPath(QStringLiteral("/api/update"));
    QUrlQuery query;
    if (!channel.trimmed().isEmpty()) {
        query.addQueryItem(QStringLiteral("channel"), channel.trimmed().toLower());
    }
    url.setQuery(query);
    return url;
}

void LicenseClient::postActivate(QString licenseKey, QString machineId)
{
    QUrl url = serverUrl_;
    // NOTE: must NOT be "/api/activate" — Cloudflare's free-plan WordPress managed rule
    // (wp-activate.php heuristic) 403-blocks any path containing "activate" at the edge,
    // and the free plan can't skip it. The Lambda aliases this neutral path to the same
    // handle_activate(). See LicenseClient endpoint history.
    url.setPath(QStringLiteral("/api/license/redeem"));

    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    request.setRawHeader("Accept", "application/json");
    // Cloudflare Bot Fight Mode (free tier) blocks bare/curl-style agents; a
    // browser-shaped UA with a platform token passes. Keep this in sync with
    // OrionUpdater's downloader.
    request.setRawHeader("User-Agent",
                         QByteArrayLiteral("OrionLauncher/") + QByteArrayLiteral(ORION_NATIVE_VERSION)
                             + QByteArrayLiteral(" (Windows NT 10.0; Win64; x64)"));
    request.setTransferTimeout(15'000);
    applyStrictTls(request);

    const QString requestNonce = QUuid::createUuid().toString(QUuid::WithoutBraces);
    const std::time_t unixNow = std::time(nullptr);
    const qint64 requestTimestamp = unixNow > 0
        ? static_cast<qint64>(unixNow)
        : QDateTime::currentDateTimeUtc().toSecsSinceEpoch();
    request.setRawHeader("X-Orion-Request-Nonce", requestNonce.toUtf8());
    request.setRawHeader("X-Orion-Request-Timestamp", QByteArray::number(requestTimestamp));
    request.setRawHeader("X-Orion-Request-Id", QUuid::createUuid().toString(QUuid::WithoutBraces).toUtf8());

    QJsonObject body;
    body.insert(QStringLiteral("license_key"), licenseKey.trimmed());
    body.insert(QStringLiteral("key"), licenseKey.trimmed());
    body.insert(QStringLiteral("machine_id"), machineId);
    body.insert(QStringLiteral("client_version"), QStringLiteral(ORION_NATIVE_VERSION));
    body.insert(QStringLiteral("fingerprint_version"), 2);
    body.insert(QStringLiteral("request_nonce"), requestNonce);
    body.insert(QStringLiteral("request_timestamp"), requestTimestamp);

    auto* reply = network_.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::encrypted, this, [this, reply]() {
        if (pinnedCertSha256_.isEmpty()) {
            return;
        }
        QString detail;
        if (!replyMatchesPinnedCertificate(reply, &detail)) {
            reply->setProperty("orion_pin_failed", true);
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::sslErrors, this, [this, reply](const QList<QSslError>& errors) {
        Q_UNUSED(errors);
        reply->abort();
        LicenseResult result;
        result.message = QStringLiteral("TLS verification failed.");
        emit activationFinished(result);
    });

    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        activationInFlight_ = false;
        LicenseResult result;
        const auto guard = qScopeGuard([reply]() { reply->deleteLater(); });
        if (reply->error() != QNetworkReply::NoError) {
            const auto body = reply->readAll();
            const auto doc = QJsonDocument::fromJson(body);
            const auto obj = doc.object();
            if (reply->property("orion_pin_failed").toBool()) {
                result.message = QStringLiteral("Pinned server certificate did not match.");
            } else if (reply->error() == QNetworkReply::OperationCanceledError || reply->error() == QNetworkReply::TimeoutError) {
                result.message = QStringLiteral("License request timed out. Use an NVDEV local key in the dev checkout or check server connectivity.");
            } else if (obj.contains(QStringLiteral("message")) || obj.contains(QStringLiteral("error"))) {
                result.message = obj.value(QStringLiteral("message")).toString(obj.value(QStringLiteral("error")).toString(reply->errorString()));
            } else {
                result.message = reply->errorString();
            }
            emit activationFinished(result);
            return;
        }

        if (!pinnedCertSha256_.isEmpty()) {
            QString detail;
            if (!replyMatchesPinnedCertificate(reply, &detail)) {
                result.message = QStringLiteral("Pinned server certificate did not match.");
                emit activationFinished(result);
                return;
            }
        }

        result = parseActivateResponse(reply->readAll());
        emit activationFinished(result);
    });
}

void LicenseClient::postValidate(QString licenseKey, QString machineId)
{
    QUrl url = serverUrl_;
    url.setPath(QStringLiteral("/api/license/check"));   // Cloudflare-safe (no "activate" in the path)

    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    request.setRawHeader("Accept", "application/json");
    request.setRawHeader("User-Agent",
                         QByteArrayLiteral("OrionLauncher/") + QByteArrayLiteral(ORION_NATIVE_VERSION)
                             + QByteArrayLiteral(" (Windows NT 10.0; Win64; x64)"));
    request.setTransferTimeout(15'000);
    applyStrictTls(request);

    QJsonObject body;
    body.insert(QStringLiteral("license_key"), licenseKey.trimmed());
    body.insert(QStringLiteral("key"), licenseKey.trimmed());
    body.insert(QStringLiteral("machine_id"), machineId);
    body.insert(QStringLiteral("client_version"), QStringLiteral(ORION_NATIVE_VERSION));

    auto* reply = network_.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::encrypted, this, [this, reply]() {
        if (pinnedCertSha256_.isEmpty()) {
            return;
        }
        QString detail;
        if (!replyMatchesPinnedCertificate(reply, &detail)) {
            reply->setProperty("orion_pin_failed", true);
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::sslErrors, this, [reply](const QList<QSslError>& errors) {
        Q_UNUSED(errors);
        reply->abort();
    });

    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        validationInFlight_ = false;
        LicenseResult result;
        const auto guard = qScopeGuard([reply]() { reply->deleteLater(); });
        result = parseLicenseCheckResponse(reply->readAll());
        if (reply->property("orion_pin_failed").toBool()) {
            // A pin failure is a transport failure, not a server verdict: never kill.
            result.error.clear();
        }
        result.ok = result.ok && reply->error() == QNetworkReply::NoError;
        emit validationFinished(result);
    });
}

void LicenseClient::getVersion()
{
    QUrl url = serverUrl_;
    url.setPath(QStringLiteral("/api/version"));
    url.setQuery(QString());

    QNetworkRequest request(url);
    request.setRawHeader("Accept", "application/json");
    // Cloudflare Bot Fight Mode (free tier) blocks bare/curl-style agents; a
    // browser-shaped UA with a platform token passes. Keep this in sync with
    // OrionUpdater's downloader.
    request.setRawHeader("User-Agent",
                         QByteArrayLiteral("OrionLauncher/") + QByteArrayLiteral(ORION_NATIVE_VERSION)
                             + QByteArrayLiteral(" (Windows NT 10.0; Win64; x64)"));
    // 15s (not 7s): the first request after idle pays fresh TLS + Cloudflare +
    // API Gateway + Lambda cold start, which exceeded 7s and flipped the Patch
    // pill to "API offline" while the backend was healthy.
    request.setTransferTimeout(15'000);
    applyStrictTls(request);

    auto* reply = network_.get(request);
    connect(reply, &QNetworkReply::encrypted, this, [this, reply]() {
        if (pinnedCertSha256_.isEmpty()) {
            return;
        }
        QString detail;
        if (!replyMatchesPinnedCertificate(reply, &detail)) {
            reply->setProperty("orion_pin_failed", true);
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::sslErrors, this, [reply](const QList<QSslError>& errors) {
        Q_UNUSED(errors);
        reply->abort();
    });

    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        versionCheckInFlight_ = false;
        VersionResult result;
        const auto guard = qScopeGuard([reply]() { reply->deleteLater(); });

        if (reply->error() != QNetworkReply::NoError) {
            const auto body = reply->readAll();
            const auto parsed = parseVersionResponse(body);
            result.message = reply->property("orion_pin_failed").toBool()
                ? QStringLiteral("Pinned server certificate did not match.")
                : parsed.message.isEmpty()
                ? QStringLiteral("Backend version check unavailable: %1").arg(reply->errorString())
                : parsed.message;
            emit versionCheckFinished(result);
            return;
        }

        if (!pinnedCertSha256_.isEmpty()) {
            QString detail;
            if (!replyMatchesPinnedCertificate(reply, &detail)) {
                result.message = QStringLiteral("Pinned server certificate did not match.");
                emit versionCheckFinished(result);
                return;
            }
        }

        result = parseVersionResponse(reply->readAll());
        emit versionCheckFinished(result);
    });
}

void LicenseClient::getUpdate(QString currentVersion, QString channel)
{
    QUrl url = serverUrl_;
    url.setPath(QStringLiteral("/api/update"));
    QUrlQuery query;
    if (!currentVersion.trimmed().isEmpty()) {
        query.addQueryItem(QStringLiteral("client_version"), currentVersion.trimmed());
    }
    if (!channel.trimmed().isEmpty()) {
        query.addQueryItem(QStringLiteral("channel"), channel.trimmed().toLower());
    }
    url.setQuery(query);

    QNetworkRequest request(url);
    request.setRawHeader("Accept", "application/json");
    // Cloudflare Bot Fight Mode (free tier) blocks bare/curl-style agents; a
    // browser-shaped UA with a platform token passes. Keep this in sync with
    // OrionUpdater's downloader.
    request.setRawHeader("User-Agent",
                         QByteArrayLiteral("OrionLauncher/") + QByteArrayLiteral(ORION_NATIVE_VERSION)
                             + QByteArrayLiteral(" (Windows NT 10.0; Win64; x64)"));
    // 15s (not 7s): the first request after idle pays fresh TLS + Cloudflare +
    // API Gateway + Lambda cold start, which exceeded 7s and flipped the Patch
    // pill to "API offline" while the backend was healthy.
    request.setTransferTimeout(15'000);
    applyStrictTls(request);

    auto* reply = network_.get(request);
    connect(reply, &QNetworkReply::encrypted, this, [this, reply]() {
        if (pinnedCertSha256_.isEmpty()) {
            return;
        }
        QString detail;
        if (!replyMatchesPinnedCertificate(reply, &detail)) {
            reply->setProperty("orion_pin_failed", true);
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::sslErrors, this, [reply](const QList<QSslError>& errors) {
        Q_UNUSED(errors);
        reply->abort();
    });

    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        updateCheckInFlight_ = false;
        const auto guard = qScopeGuard([reply]() { reply->deleteLater(); });

        if (reply->error() != QNetworkReply::NoError) {
            // Fail soft: the update endpoint being unreachable (or not yet
            // deployed) must not block the app. Report ok == false so the caller
            // can fall back to the backend health check.
            UpdateManifest soft;
            soft.message = reply->property("orion_pin_failed").toBool()
                ? QStringLiteral("Pinned server certificate did not match.")
                : QStringLiteral("Update check unavailable: %1").arg(reply->errorString());
            emit updateCheckFinished(soft);
            return;
        }

        if (!pinnedCertSha256_.isEmpty()) {
            QString detail;
            if (!replyMatchesPinnedCertificate(reply, &detail)) {
                UpdateManifest soft;
                soft.message = QStringLiteral("Pinned server certificate did not match.");
                emit updateCheckFinished(soft);
                return;
            }
        }

        emit updateCheckFinished(parseUpdateManifest(reply->readAll()));
    });
}

} // namespace orion
