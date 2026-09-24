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
    // Contract §5: /api/version carries `motd` when one is set and unexpired. Read it
    // before the identity checks so a notice survives even a failed health verdict.
    result.motd = parseMotd(obj.value(QStringLiteral("motd")));
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
    result.canonicalLicenseKey = obj.value(QStringLiteral("canonical_license_key")).toString().trimmed().toUpper();
    // CRIT-1: keep the signed session token's identity CLIENT-SIDE (it used to
    // be discarded). The backend returns "tid" (handle_activate); accept the
    // documented "token_id" spelling too.
    result.tokenId = obj.value(QStringLiteral("tid")).toString(obj.value(QStringLiteral("token_id")).toString());
    result.tokenExpiresEpochS = static_cast<qint64>(obj.value(QStringLiteral("expires")).toDouble(0.0));
    result.leaseSig = obj.value(QStringLiteral("sig")).toString().toUtf8();
    result.error = obj.value(QStringLiteral("error")).toString();
    result.message = obj.value(QStringLiteral("message")).toString(
        obj.value(QStringLiteral("error")).toString(result.ok ? QStringLiteral("License activated.") : QStringLiteral("Activation failed.")));
    // Contract §5: `version_blocked` names the oldest accepted client; `motd` is
    // read here too in case the server attaches it to the activation verdict.
    result.minClientVersion = obj.value(QStringLiteral("min_client_version")).toString();
    result.motd = parseMotd(obj.value(QStringLiteral("motd")));
    // Customer Profile page data. Absent on the current live Lambda -> known=false.
    result.profile = parseLicenseProfile(obj.value(QStringLiteral("profile")));
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
    // Contract §5: version gate + MOTD ride the heartbeat.
    result.minClientVersion = obj.value(QStringLiteral("min_client_version")).toString();
    result.motd = parseMotd(obj.value(QStringLiteral("motd")));
    // The heartbeat is what keeps the Profile page's days-left fresh.
    result.profile = parseLicenseProfile(obj.value(QStringLiteral("profile")));
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

LicenseProfile parseLicenseProfile(const QJsonValue& value)
{
    LicenseProfile profile;
    if (!value.isObject()) {
        return profile;   // absent == unknown; the caller keeps what it had
    }
    const QJsonObject obj = value.toObject();

    // DynamoDB numbers arrive as JSON numbers through json.dumps(default=
    // decimal_default), but a legacy row can stringify one. Accept both rather
    // than silently reporting 0 days left to a paying customer.
    const auto number = [](const QJsonValue& v) -> qint64 {
        if (v.isDouble()) {
            return static_cast<qint64>(v.toDouble(0.0));
        }
        if (v.isString()) {
            bool ok = false;
            const qint64 parsed = v.toString().trimmed().toLongLong(&ok);
            return ok ? parsed : 0;
        }
        return 0;
    };
    const auto count = [&number](const QJsonValue& v) {
        const qint64 raw = number(v);
        if (raw <= 0) {
            return 0;
        }
        return static_cast<int>(raw < 1'000'000 ? raw : 1'000'000);
    };

    profile.known = true;
    profile.discordUserId = obj.value(QStringLiteral("discord_user_id")).toString().trimmed();
    profile.discordUsername = obj.value(QStringLiteral("discord_username")).toString().trimmed();
    profile.plan = obj.value(QStringLiteral("plan")).toString().trimmed();
    const qint64 expiry = number(obj.value(QStringLiteral("expiry")));
    profile.expiryEpochS = expiry > 0 ? expiry : 0;
    const qint64 activatedAt = number(obj.value(QStringLiteral("activated_at")));
    profile.activatedAtEpochS = activatedAt > 0 ? activatedAt : 0;

    const QJsonObject resets = obj.value(QStringLiteral("hwid_resets")).toObject();
    profile.hwidResetsUsed = count(resets.value(QStringLiteral("used")));
    profile.hwidResetsFreeTotal = count(resets.value(QStringLiteral("free_total")));
    profile.hwidPaidCredits = count(resets.value(QStringLiteral("paid_credits")));
    // Trust the server's free_remaining, but never let a stale/garbled pair
    // render "4 of 3 free remaining": the derived value is the ceiling.
    const int derivedRemaining =
        profile.hwidResetsFreeTotal > profile.hwidResetsUsed
            ? profile.hwidResetsFreeTotal - profile.hwidResetsUsed
            : 0;
    const int reportedRemaining = count(resets.value(QStringLiteral("free_remaining")));
    profile.hwidResetsFreeRemaining =
        reportedRemaining < derivedRemaining ? reportedRemaining : derivedRemaining;
    return profile;
}

LicenseMotd parseMotd(const QJsonValue& value)
{
    LicenseMotd motd;
    if (!value.isObject()) {
        return motd;
    }
    const QJsonObject obj = value.toObject();
    motd.text = obj.value(QStringLiteral("text")).toString().trimmed();
    if (motd.text.isEmpty()) {
        return motd;   // no text == no notice, whatever else is set
    }

    const QString level = obj.value(QStringLiteral("level")).toString().trimmed().toLower();
    if (level == QLatin1String("warn") || level == QLatin1String("warning")) {
        motd.level = QStringLiteral("warn");
    } else if (level == QLatin1String("maint") || level == QLatin1String("maintenance")) {
        motd.level = QStringLiteral("maint");
    } else {
        motd.level = QStringLiteral("info");
    }

    // `until`: Unix seconds per the contract ("Timestamps are Unix seconds"); a
    // numeric string or an ISO-8601 stamp is tolerated so an owner-tool typo cannot
    // pin a notice forever. Unparseable -> 0 (no expiry).
    const QJsonValue until = obj.value(QStringLiteral("until"));
    if (until.isDouble()) {
        motd.untilEpochS = static_cast<qint64>(until.toDouble(0.0));
    } else if (until.isString()) {
        const QString raw = until.toString().trimmed();
        bool numeric = false;
        const qint64 asNumber = raw.toLongLong(&numeric);
        if (numeric) {
            motd.untilEpochS = asNumber;
        } else {
            const QDateTime stamp = QDateTime::fromString(raw, Qt::ISODate);
            motd.untilEpochS = stamp.isValid() ? stamp.toSecsSinceEpoch() : 0;
        }
    }
    if (motd.untilEpochS < 0) {
        motd.untilEpochS = 0;
    }
    return motd;
}

bool isLicenseKillCode(const QString& code)
{
    return code == QLatin1String("service_disabled") || code == QLatin1String("revoked")
        || code == QLatin1String("expired") || code == QLatin1String("device_mismatch")
        || code == QLatin1String("invalid_key") || code == QLatin1String("inactive")
        || code == QLatin1String("frozen") || code == QLatin1String("blacklisted")
        || code == QLatin1String("version_blocked")
        || code == QLatin1String("subscription_required");
}

bool isRememberedSignInRefusalCode(const QString& code)
{
    static const char* const kRefusals[] = {
        "invalid_key", "revoked", "license_revoked", "expired", "license_expired",
        "inactive", "license_invalid_status", "frozen", "blacklisted", "device_mismatch",
        "device_limit_reached", "subscription_required", "discord_signin_required",
        "trial_used", "pair_invalid",
    };
    for (const char* refusal : kRefusals) {
        if (code == QLatin1String(refusal)) {
            return true;
        }
    }
    return false;
}

QString licenseErrorUserText(const LicenseResult& result, const QString& fallback)
{
    const QString& code = result.error;
    // [2026-09-23 RT-MED-10 / P-A] The owner's kill switch is a service pause, never a network
    // problem; and an unreadable kill state / config is a retriable outage the launcher rides out
    // on its existing bounded lease.
    if (code == QLatin1String("service_disabled") || code.startsWith(QLatin1String("service_disabled:"))) {
        return QStringLiteral("Venice is paused by the service right now. Nothing is wrong with your PC or internet.");
    }
    if (code == QLatin1String("kill_state_unavailable") || code == QLatin1String("config_unavailable")) {
        return QStringLiteral("Venice's service can't confirm your access right now. Retrying automatically.");
    }
    if (code == QLatin1String("frozen")) {
        return QStringLiteral("Your subscription is paused — contact support.");
    }
    if (code == QLatin1String("blacklisted")) {
        return QStringLiteral("This device is blocked.");
    }
    if (code == QLatin1String("subscription_required")) {
        return QStringLiteral("An active Venice subscription tied to your Discord account is required. Start your trial or subscribe at zaeorion.com, or open a ticket.");
    }
    if (code == QLatin1String("version_blocked")) {
        const QString minimum = result.minClientVersion.trimmed();
        return minimum.isEmpty()
            ? QStringLiteral("Update required — this version is no longer allowed.")
            : QStringLiteral("Update required — this version is no longer allowed (minimum version %1).")
                  .arg(minimum);
    }
    // [CL2-P8-006 2026-09-23] The server rejects a request_timestamp more than 300 s
    // off with the bare code and no message; the parser then shows `timestamp_expired`
    // verbatim. The code itself stays in result.error for the log line.
    if (code == QLatin1String("timestamp_expired")) {
        return QStringLiteral("Your PC clock is off. Turn on 'Set time automatically' in Windows Date & Time settings, then restart Venice.");
    }
    // [CL2-P8-008 2026-09-23] Activation's device_mismatch carries no message (the
    // heartbeat's does); one plain line for both so a PC rename / second PC is clear.
    if (code == QLatin1String("device_mismatch")) {
        return QStringLiteral("This licence is linked to a different PC. Use /hwid_reset in Discord or open a ticket.");
    }
    // Every other code: the server's prose. The parsers fall back to the bare code
    // when the server sent no `message`, which is still better than silence.
    if (!result.message.trimmed().isEmpty()) {
        return result.message;
    }
    return fallback;
}

QJsonObject licenseActivateRequestBody(const QString& licenseKey,
                                       const QString& machineId,
                                       const QString& requestNonce,
                                       qint64 requestTimestamp)
{
    QJsonObject body;
    body.insert(QStringLiteral("license_key"), licenseKey.trimmed());
    body.insert(QStringLiteral("key"), licenseKey.trimmed());
    body.insert(QStringLiteral("machine_id"), machineId);
    body.insert(QStringLiteral("client_version"), QStringLiteral(ORION_NATIVE_VERSION));
    body.insert(QStringLiteral("fingerprint_version"), 2);
    body.insert(QStringLiteral("request_nonce"), requestNonce);
    body.insert(QStringLiteral("request_timestamp"), requestTimestamp);
    return body;
}

QJsonObject licenseCheckRequestBody(const QString& licenseKey, const QString& machineId)
{
    QJsonObject body;
    body.insert(QStringLiteral("license_key"), licenseKey.trimmed());
    body.insert(QStringLiteral("key"), licenseKey.trimmed());
    body.insert(QStringLiteral("machine_id"), machineId);
    // Version gate (contract §5): the heartbeat carries client_version exactly as
    // activation does so the server can answer version_blocked mid-session.
    body.insert(QStringLiteral("client_version"), QStringLiteral(ORION_NATIVE_VERSION));
    return body;
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

    const QJsonObject body = licenseActivateRequestBody(licenseKey, machineId, requestNonce, requestTimestamp);

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
#ifdef ORION_PRODUCTION_BUILD
                // Production must not carry instructions or marker strings for
                // a developer-only offline key path. The local hook is compiled
                // out in OrionAppController; keep its customer error copy out too.
                result.message = QStringLiteral("License request timed out. Check your connection and try again.");
#else
                result.message = QStringLiteral("License request timed out. Use an NVDEV local key in the dev checkout or check server connectivity.");
#endif
            } else if (obj.contains(QStringLiteral("message")) || obj.contains(QStringLiteral("error"))) {
                // A structured verdict behind an HTTP 4xx (the backend's err() helper
                // answers 403/426 with a JSON body): keep the error CODE and
                // min_client_version so the controller can map version_blocked /
                // frozen / blacklisted to their own copy instead of raw prose.
                const LicenseResult verdict = parseActivateResponse(body);
                result.error = verdict.error;
                result.minClientVersion = verdict.minClientVersion;
                result.motd = verdict.motd;
                result.message = verdict.message.isEmpty() ? reply->errorString() : verdict.message;
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

    const QJsonObject body = licenseCheckRequestBody(licenseKey, machineId);

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
            if (reply->property("orion_pin_failed").toBool()) {
                soft.message = QStringLiteral("Pinned server certificate did not match.");
            } else {
                // [2026-09-21] A 4xx/5xx with a JSON envelope is the SERVER talking
                // (e.g. 404 {"ok":false,"error":"no manifest published"} before the
                // first release). Log its reason instead of Qt's truncated
                // "Error transferring ... server replied:", which reads like an outage.
                const UpdateManifest envelope = parseUpdateManifest(reply->readAll());
                const bool serverSpoke = !envelope.ok && !envelope.message.isEmpty()
                    && envelope.message != QStringLiteral("Update manifest is not valid JSON.");
                soft.message = serverSpoke
                    ? QStringLiteral("Update check: server says \"%1\" (HTTP %2)")
                          .arg(envelope.message)
                          .arg(reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt())
                    : QStringLiteral("Update check unavailable: %1").arg(reply->errorString());
            }
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
