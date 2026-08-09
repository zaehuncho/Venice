#include "AdminToolController.h"

#include "NetworkSecurity.h"
#include "UpdateManifest.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QFile>
#include <QtCore/QProcess>
#include <QtCore/QTextStream>
#include <QtCore/QTimer>
#include <QtCore/QUrlQuery>
#include <QtCore/QUuid>
#include <QtCore/qscopeguard.h>
#include <QtNetwork/QNetworkReply>
#include <QtNetwork/QNetworkRequest>
#include <QtNetwork/QSslConfiguration>
#include <QtWidgets/QApplication>

#include <ctime>

namespace orion {

namespace {

QByteArray userAgent()
{
    return QByteArrayLiteral("OrionAdminTool/") + QByteArrayLiteral(ORION_NATIVE_VERSION)
        + QByteArrayLiteral(" (Windows NT 10.0; Win64; x64)");
}

QString suffix(const QString& value, int count = 8)
{
    const QString trimmed = value.trimmed();
    return trimmed.isEmpty() ? QStringLiteral("(none)") : trimmed.right(count);
}

QString actionTargetBodyKey(const QString& value)
{
    const QString trimmed = value.trimmed();
    if (trimmed.startsWith(QLatin1String("staff_"))) {
        return QStringLiteral("staff_id");
    }
    return QStringLiteral("discord_user_id");
}

QString normalizeSecretInput(QString value)
{
    QString normalized = value.trimmed();
    if (normalized.size() >= 2) {
        const QChar first = normalized.front();
        const QChar last = normalized.back();
        if ((first == QLatin1Char('"') && last == QLatin1Char('"'))
            || (first == QLatin1Char('\'') && last == QLatin1Char('\''))) {
            normalized = normalized.mid(1, normalized.size() - 2).trimmed();
        }
    }
    return normalized;
}

bool looksLikeCommandInput(const QString& value)
{
    const QString lower = value.trimmed().toLower();
    return lower.startsWith(QLatin1String("aws "))
        || lower.startsWith(QLatin1String("aws.exe "))
        || lower.contains(QLatin1String("ssm get-parameter"))
        || lower.contains(QLatin1String("set-clipboard"))
        || lower.contains(QLatin1String("--with-decryption"));
}

void appendAdminDiagnostic(const QString& rootDir, const QString& event, const QString& detail = QString())
{
    QDir dir(rootDir + QStringLiteral("/logs/diagnostics"));
    dir.mkpath(QStringLiteral("."));
    QFile file(dir.filePath(QStringLiteral("admin_tool.log")));
    if (!file.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text)) {
        return;
    }
    QTextStream stream(&file);
    stream << QDateTime::currentDateTimeUtc().toString(Qt::ISODate)
           << " " << event;
    if (!detail.isEmpty()) {
        stream << " " << detail.left(240).replace(QLatin1Char('\n'), QLatin1Char(' '));
    }
    stream << '\n';
}

SecurityStatus adminToolSecurityStatus(SecurityStatus status)
{
    // Owner/Staff tools do not consume gameplay settings. Keep release-manifest,
    // debugger, and analysis-tool locks, but do not block privileged auth because
    // a packaged admin tool has no launcher settings.json/signature pair.
    if (status.securityLockActive
        && status.securityLockReason == QLatin1String("Settings signature missing or invalid")) {
        status.securityLockActive = false;
        status.securityLockReason.clear();
        status.message = QStringLiteral("Admin package integrity verified.");
        if (status.integrityState.trimmed().isEmpty() || status.integrityState == QLatin1String("Unchecked")) {
            status.integrityState = status.releaseManifestValid
                ? QStringLiteral("Release integrity verified")
                : QStringLiteral("Security checked");
        }
    }
    return status;
}

} // namespace

AdminToolController::AdminToolController(QString rootDir, Mode mode, QObject* parent)
    : QObject(parent),
      rootDir_(std::move(rootDir)),
      mode_(mode),
      security_(rootDir_, this),
      licenseClient_(this)
{
    licenseClient_.setServerUrl(QUrl(apiBase()));
    connect(&licenseClient_, &LicenseClient::updateCheckFinished, this, [this](const UpdateManifest& manifest) {
        if (!manifest.ok) {
            updateState_ = manifest.message.isEmpty() ? QStringLiteral("Update unavailable") : manifest.message;
            updateAvailable_ = false;
            emit stateChanged();
            return;
        }
        latestVersion_ = manifest.version;
        const UpdateDecision decision = evaluateUpdate(appVersion(), manifest);
        updateAvailable_ = decision == UpdateDecision::UpdateAvailable
            || decision == UpdateDecision::MandatoryUpdate
            || decision == UpdateDecision::ClientBlocked;
        updateState_ = updateAvailable_
            ? QStringLiteral("Update available: %1").arg(latestVersion_)
            : QStringLiteral("Current");
        emit stateChanged();
    });
    refreshSecurity();
}

QString AdminToolController::mode() const
{
    return ownerMode() ? QStringLiteral("owner") : QStringLiteral("staff");
}

QString AdminToolController::machineIdSuffix() const
{
    return suffix(machineId(), 10);
}

void AdminToolController::ownerLogin(const QString& adminSecret)
{
    appendAdminDiagnostic(rootDir_, QStringLiteral("owner_login_clicked"));
    if (!ownerMode()) {
        setStatus(QStringLiteral("Owner login is unavailable in staff build."));
        return;
    }
    if (!requireUsableSecurity()) {
        return;
    }
    const QString secret = normalizeSecretInput(adminSecret);
    if (looksLikeCommandInput(secret)) {
        setStatus(QStringLiteral("Paste the secret value, not the AWS command."));
        appendAdminDiagnostic(rootDir_, QStringLiteral("owner_login_rejected_command_input"));
        return;
    }
    if (secret.isEmpty()) {
        setStatus(QStringLiteral("Admin secret is required."));
        return;
    }
    ownerSecret_ = secret;
    getJson(QStringLiteral("/api/admin/whoami"), {}, AuthKind::OwnerSecret);
}

void AdminToolController::staffEnroll(const QString& discordUserId, const QString& enrollmentKey)
{
    appendAdminDiagnostic(rootDir_, QStringLiteral("staff_enroll_clicked"));
    if (ownerMode()) {
        setStatus(QStringLiteral("Staff enrollment is unavailable in owner build."));
        return;
    }
    if (!requireUsableSecurity()) {
        return;
    }
    QJsonObject body = freshnessFields();
    body.insert(QStringLiteral("discord_user_id"), discordUserId.trimmed());
    body.insert(QStringLiteral("enrollment_key"), enrollmentKey.trimmed());
    body.insert(QStringLiteral("machine_id"), machineId());
    postJson(QStringLiteral("/api/staff/enroll"), body, AuthKind::None);
}

void AdminToolController::staffLogin(const QString& discordUserId)
{
    appendAdminDiagnostic(rootDir_, QStringLiteral("staff_login_clicked"));
    if (ownerMode()) {
        setStatus(QStringLiteral("Staff login is unavailable in owner build."));
        return;
    }
    if (!requireUsableSecurity()) {
        return;
    }
    QJsonObject body = freshnessFields();
    body.insert(QStringLiteral("discord_user_id"), discordUserId.trimmed());
    body.insert(QStringLiteral("machine_id"), machineId());
    postJson(QStringLiteral("/api/staff/login"), body, AuthKind::None);
}

void AdminToolController::logout()
{
    ownerSecret_.clear();
    staffToken_.clear();
    staffDiscordId_.clear();
    role_.clear();
    staffName_.clear();
    authenticated_ = false;
    setResult(QString());
    setStatus(QStringLiteral("Signed out."));
    emit stateChanged();
}

void AdminToolController::createStaff(const QString& discordUserId, const QString& displayName, const QString& role)
{
    if (!ownerMode() || !authenticated_) {
        setStatus(QStringLiteral("Owner authentication is required."));
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("create"));
    body.insert(QStringLiteral("discord_user_id"), discordUserId.trimmed());
    body.insert(QStringLiteral("display_name"), displayName.trimmed());
    body.insert(QStringLiteral("role"), role.trimmed().toLower());
    body.insert(QStringLiteral("reason"), QStringLiteral("owner staff create"));
    postJson(QStringLiteral("/api/admin/staff"), body, AuthKind::OwnerSecret);
}

void AdminToolController::disableStaff(const QString& staffOrDiscordId, const QString& reason)
{
    if (!ownerMode() || !authenticated_) {
        setStatus(QStringLiteral("Owner authentication is required."));
        return;
    }
    if (reason.trimmed().isEmpty()) {
        setStatus(QStringLiteral("Reason is required."));
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("disable"));
    body.insert(actionTargetBodyKey(staffOrDiscordId), staffOrDiscordId.trimmed());
    body.insert(QStringLiteral("reason"), reason.trimmed());
    postJson(QStringLiteral("/api/admin/staff"), body, AuthKind::OwnerSecret);
}

void AdminToolController::resetStaffMachine(const QString& staffOrDiscordId, const QString& reason)
{
    if (!ownerMode() || !authenticated_) {
        setStatus(QStringLiteral("Owner authentication is required."));
        return;
    }
    if (reason.trimmed().isEmpty()) {
        setStatus(QStringLiteral("Reason is required."));
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("reset_machine"));
    body.insert(actionTargetBodyKey(staffOrDiscordId), staffOrDiscordId.trimmed());
    body.insert(QStringLiteral("reason"), reason.trimmed());
    postJson(QStringLiteral("/api/admin/staff"), body, AuthKind::OwnerSecret);
}

void AdminToolController::reissueStaffEnrollment(const QString& staffOrDiscordId, const QString& reason)
{
    if (!ownerMode() || !authenticated_) {
        setStatus(QStringLiteral("Owner authentication is required."));
        return;
    }
    if (reason.trimmed().isEmpty()) {
        setStatus(QStringLiteral("Reason is required."));
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("reissue_enrollment"));
    body.insert(actionTargetBodyKey(staffOrDiscordId), staffOrDiscordId.trimmed());
    body.insert(QStringLiteral("reason"), reason.trimmed());
    postJson(QStringLiteral("/api/admin/staff"), body, AuthKind::OwnerSecret);
}

void AdminToolController::lookupLicense(const QString& licenseKey)
{
    if (!authenticated_) {
        setStatus(QStringLiteral("Authentication is required."));
        return;
    }
    QJsonObject query;
    query.insert(QStringLiteral("key"), licenseKey.trimmed());
    getJson(ownerMode() ? QStringLiteral("/api/admin/license") : QStringLiteral("/api/staff/license"),
            query,
            ownerMode() ? AuthKind::OwnerSecret : AuthKind::StaffToken);
}

void AdminToolController::resetLicenseHwid(const QString& licenseKey, const QString& reason)
{
    if (!authenticated_) {
        setStatus(QStringLiteral("Authentication is required."));
        return;
    }
    if (reason.trimmed().isEmpty()) {
        setStatus(QStringLiteral("Reason is required."));
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("license_key"), licenseKey.trimmed());
    body.insert(QStringLiteral("action"), QStringLiteral("reset_machine"));
    body.insert(QStringLiteral("reason"), reason.trimmed());
    postJson(ownerMode() ? QStringLiteral("/api/admin/license") : QStringLiteral("/api/staff/license"),
             body,
             ownerMode() ? AuthKind::OwnerSecret : AuthKind::StaffToken);
}

void AdminToolController::deactivateLicense(const QString& licenseKey, const QString& reason)
{
    if (!authenticated_) {
        setStatus(QStringLiteral("Authentication is required."));
        return;
    }
    if (reason.trimmed().isEmpty()) {
        setStatus(QStringLiteral("Reason is required."));
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("license_key"), licenseKey.trimmed());
    body.insert(QStringLiteral("action"), QStringLiteral("deactivate"));
    body.insert(QStringLiteral("reason"), reason.trimmed());
    postJson(ownerMode() ? QStringLiteral("/api/admin/license") : QStringLiteral("/api/staff/license"),
             body,
             ownerMode() ? AuthKind::OwnerSecret : AuthKind::StaffToken);
}

void AdminToolController::refreshKillSwitch()
{
    if (!ownerMode() || !authenticated_) {
        setStatus(QStringLiteral("Owner authentication is required."));
        return;
    }
    getJson(QStringLiteral("/api/admin/status"), QJsonObject{}, AuthKind::OwnerSecret);
}

void AdminToolController::engageKillSwitch(const QString& reason)
{
    if (!ownerMode() || !authenticated_) {
        setStatus(QStringLiteral("Owner authentication is required."));
        return;
    }
    if (reason.trimmed().isEmpty()) {
        setStatus(QStringLiteral("Reason is required."));
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("target_type"), QStringLiteral("global"));
    body.insert(QStringLiteral("reason"), reason.trimmed());
    postJson(QStringLiteral("/api/admin/kill"), body, AuthKind::OwnerSecret);
}

void AdminToolController::releaseKillSwitch()
{
    if (!ownerMode() || !authenticated_) {
        setStatus(QStringLiteral("Owner authentication is required."));
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("target_type"), QStringLiteral("global"));
    postJson(QStringLiteral("/api/admin/unkill"), body, AuthKind::OwnerSecret);
}

void AdminToolController::checkUpdate()
{
    updateState_ = QStringLiteral("Checking...");
    emit stateChanged();
    licenseClient_.checkUpdate(appVersion(), QStringLiteral("stable"));
}

void AdminToolController::startUpdate()
{
    if (!updateAvailable_) {
        setStatus(QStringLiteral("No update is available."));
        return;
    }
    startUpdaterForSelf();
}

void AdminToolController::refreshSecurity()
{
    const SecurityStatus status = adminToolSecurityStatus(security_.evaluate());
    securityLockActive_ = status.securityLockActive;
    securityState_ = status.securityLockActive
        ? QStringLiteral("Locked: %1").arg(status.securityLockReason)
        : status.integrityState;
    if (securityState_.trimmed().isEmpty()) {
        securityState_ = QStringLiteral("Security checked");
    }
    reportTamperIfAuthenticated(status);
    emit stateChanged();
}

void AdminToolController::clearResult()
{
    setResult(QString());
}

void AdminToolController::setBusy(bool value)
{
    if (busy_ == value) {
        return;
    }
    busy_ = value;
    emit stateChanged();
}

void AdminToolController::setStatus(QString value)
{
    statusMessage_ = std::move(value);
    emit stateChanged();
}

void AdminToolController::setResult(QString value)
{
    resultText_ = std::move(value);
    emit stateChanged();
}

bool AdminToolController::requireUsableSecurity()
{
    refreshSecurity();
    if (securityLockActive_) {
        setStatus(QStringLiteral("Security lock active. Privileged tool refused."));
        return false;
    }
    return true;
}

void AdminToolController::postJson(const QString& path, QJsonObject body, AuthKind authKind)
{
    if (!requireUsableSecurity()) {
        return;
    }
    setBusy(true);
    QUrl url(apiBase());
    url.setPath(path);
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    request.setRawHeader("Accept", "application/json");
    request.setRawHeader("User-Agent", userAgent());
    request.setTransferTimeout(15'000);
    applyStrictTls(request);
    if (authKind == AuthKind::OwnerSecret) {
        request.setRawHeader("X-Orion-Admin-Secret", ownerSecret_.toUtf8());
    } else if (authKind == AuthKind::StaffToken) {
        request.setRawHeader("Authorization", QByteArrayLiteral("Bearer ") + staffToken_.toUtf8());
        request.setRawHeader("X-Orion-Discord-Id", staffDiscordId_.toUtf8());
    }
    auto* reply = network_.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::encrypted, this, [this, reply, path]() {
        QString detail;
        if (!replyMatchesPinnedCertificate(reply, &detail)) {
            appendAdminDiagnostic(rootDir_, QStringLiteral("tls_pin_failed"), QStringLiteral("path=%1").arg(path));
            reply->setProperty("orion_pin_failed", true);
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::sslErrors, this, [this, reply, path](const QList<QSslError>&) {
        appendAdminDiagnostic(rootDir_, QStringLiteral("tls_verify_failed"), QStringLiteral("path=%1").arg(path));
        reply->abort();
    });
    connect(reply, &QNetworkReply::finished, this, [this, reply, path]() {
        const auto guard = qScopeGuard([reply]() { reply->deleteLater(); });
        const bool ok = reply->error() == QNetworkReply::NoError;
        const int httpStatus = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        appendAdminDiagnostic(rootDir_,
                              QStringLiteral("http_finished"),
                              QStringLiteral("path=%1 status=%2 net_error=%3").arg(path).arg(httpStatus).arg(static_cast<int>(reply->error())));
        if (reply->property("orion_pin_failed").toBool()) {
            setBusy(false);
            setStatus(QStringLiteral("Pinned server certificate did not match."));
            return;
        }
        handleResponse(path, reply->readAll(), httpStatus, ok);
    });
}

void AdminToolController::getJson(const QString& path, const QJsonObject& query, AuthKind authKind)
{
    if (!requireUsableSecurity()) {
        return;
    }
    setBusy(true);
    QUrl url(apiBase());
    url.setPath(path);
    QUrlQuery q;
    for (auto it = query.constBegin(); it != query.constEnd(); ++it) {
        q.addQueryItem(it.key(), it.value().toString());
    }
    url.setQuery(q);
    QNetworkRequest request(url);
    request.setRawHeader("Accept", "application/json");
    request.setRawHeader("User-Agent", userAgent());
    request.setTransferTimeout(15'000);
    applyStrictTls(request);
    if (authKind == AuthKind::OwnerSecret) {
        request.setRawHeader("X-Orion-Admin-Secret", ownerSecret_.toUtf8());
    } else if (authKind == AuthKind::StaffToken) {
        request.setRawHeader("Authorization", QByteArrayLiteral("Bearer ") + staffToken_.toUtf8());
        request.setRawHeader("X-Orion-Discord-Id", staffDiscordId_.toUtf8());
    }
    auto* reply = network_.get(request);
    connect(reply, &QNetworkReply::encrypted, this, [this, reply, path]() {
        QString detail;
        if (!replyMatchesPinnedCertificate(reply, &detail)) {
            appendAdminDiagnostic(rootDir_, QStringLiteral("tls_pin_failed"), QStringLiteral("path=%1").arg(path));
            reply->setProperty("orion_pin_failed", true);
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::sslErrors, this, [this, reply, path](const QList<QSslError>&) {
        appendAdminDiagnostic(rootDir_, QStringLiteral("tls_verify_failed"), QStringLiteral("path=%1").arg(path));
        reply->abort();
    });
    connect(reply, &QNetworkReply::finished, this, [this, reply, path]() {
        const auto guard = qScopeGuard([reply]() { reply->deleteLater(); });
        const bool ok = reply->error() == QNetworkReply::NoError;
        const int httpStatus = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        appendAdminDiagnostic(rootDir_,
                              QStringLiteral("http_finished"),
                              QStringLiteral("path=%1 status=%2 net_error=%3").arg(path).arg(httpStatus).arg(static_cast<int>(reply->error())));
        if (reply->property("orion_pin_failed").toBool()) {
            setBusy(false);
            setStatus(QStringLiteral("Pinned server certificate did not match."));
            return;
        }
        handleResponse(path, reply->readAll(), httpStatus, ok);
    });
}

void AdminToolController::handleResponse(const QString& path, const QByteArray& payload, int httpStatus, bool ok)
{
    setBusy(false);
    QJsonParseError parseError;
    const auto doc = QJsonDocument::fromJson(payload, &parseError);
    const QJsonObject obj = doc.isObject() ? doc.object() : QJsonObject{};
    if (!ok || parseError.error != QJsonParseError::NoError) {
        if (path == QLatin1String("/api/admin/whoami") && (httpStatus == 401 || httpStatus == 403)) {
            setStatus(QStringLiteral("Owner secret rejected by server."));
        } else if (path == QLatin1String("/api/admin/whoami") && httpStatus == 0) {
            setStatus(QStringLiteral("Owner auth request failed before reaching server."));
        } else {
            setStatus(QStringLiteral("Request failed (%1).").arg(httpStatus > 0 ? httpStatus : 0));
        }
        return;
    }
    if (!obj.value(QStringLiteral("ok")).toBool(false)) {
        setStatus(obj.value(QStringLiteral("message")).toString(obj.value(QStringLiteral("error")).toString(QStringLiteral("Request rejected."))));
        setResult(QString::fromUtf8(QJsonDocument(obj).toJson(QJsonDocument::Indented)));
        return;
    }

    if (path == QLatin1String("/api/admin/whoami")) {
        authenticated_ = true;
        role_ = QStringLiteral("owner");
        staffName_ = QStringLiteral("Owner");
        setStatus(QStringLiteral("Owner authenticated."));
    } else if (path == QLatin1String("/api/staff/enroll") || path == QLatin1String("/api/staff/login")) {
        authenticated_ = true;
        staffToken_ = obj.value(QStringLiteral("token")).toString();
        const QJsonObject staff = obj.value(QStringLiteral("staff")).toObject();
        role_ = staff.value(QStringLiteral("role")).toString(QStringLiteral("support"));
        staffName_ = staff.value(QStringLiteral("display_name")).toString(staff.value(QStringLiteral("discord_user_id")).toString());
        staffDiscordId_ = staff.value(QStringLiteral("discord_user_id")).toString();
        setStatus(path.endsWith(QStringLiteral("enroll")) ? QStringLiteral("Staff enrolled.") : QStringLiteral("Staff authenticated."));
    } else if (path == QLatin1String("/api/admin/kill") || path == QLatin1String("/api/admin/unkill")
               || path == QLatin1String("/api/admin/status")) {
        killSwitchEngaged_ = obj.value(QStringLiteral("global_kill")).toBool(killSwitchEngaged_);
        killSwitchReason_ = obj.value(QStringLiteral("kill_reason")).toString(
            obj.value(QStringLiteral("reason")).toString(killSwitchEngaged_ ? killSwitchReason_ : QString()));
        setStatus(path == QLatin1String("/api/admin/kill") ? QStringLiteral("Killswitch engaged.")
                  : path == QLatin1String("/api/admin/unkill") ? QStringLiteral("Killswitch released.")
                  : QStringLiteral("Killswitch status refreshed."));
    } else {
        setStatus(obj.value(QStringLiteral("message")).toString(QStringLiteral("Request completed.")));
    }

    setResult(QString::fromUtf8(QJsonDocument(obj).toJson(QJsonDocument::Indented)));
}

QJsonObject AdminToolController::freshnessFields() const
{
    const std::time_t unixNow = std::time(nullptr);
    QJsonObject obj;
    obj.insert(QStringLiteral("request_nonce"), QUuid::createUuid().toString(QUuid::WithoutBraces));
    obj.insert(QStringLiteral("request_timestamp"),
               unixNow > 0 ? static_cast<qint64>(unixNow) : QDateTime::currentDateTimeUtc().toSecsSinceEpoch());
    return obj;
}

QString AdminToolController::apiBase() const
{
    return QStringLiteral("https://api.zaeorion.com");
}

QString AdminToolController::machineId() const
{
    const QByteArray material = (security_.machineId() + QLatin1Char('|') + mode()).toUtf8();
    return QStringLiteral("admintool-") + QString::fromLatin1(QCryptographicHash::hash(material, QCryptographicHash::Sha256).toHex());
}

bool AdminToolController::devBuild() const
{
    const QString appDir = QDir::fromNativeSeparators(QCoreApplication::applicationDirPath()).toLower();
    return appDir.contains(QStringLiteral("/native_orion/build/"));
}

void AdminToolController::startUpdaterForSelf()
{
    if (devBuild()) {
        setStatus(QStringLiteral("Update refused from build tree."));
        return;
    }
    const QString installDir = QCoreApplication::applicationDirPath();
    const QString updaterPath = installDir + QStringLiteral("/OrionUpdater.exe");
    if (!QFileInfo::exists(updaterPath)) {
        setStatus(QStringLiteral("Updater missing."));
        return;
    }
    const QString exeName = QFileInfo(QCoreApplication::applicationFilePath()).fileName();
    const QStringList args = {
        QStringLiteral("--manifest-url"), licenseClient_.updateManifestUrl(QStringLiteral("stable")).toString(),
        QStringLiteral("--install-dir"), installDir,
        QStringLiteral("--launcher-pid"), QString::number(QCoreApplication::applicationPid()),
        QStringLiteral("--current-version"), appVersion(),
        QStringLiteral("--relaunch"), exeName,
    };
    if (!QProcess::startDetached(updaterPath, args, installDir)) {
        setStatus(QStringLiteral("Updater failed to start."));
        return;
    }
    QTimer::singleShot(300, qApp, &QCoreApplication::quit);
}

void AdminToolController::reportTamperIfAuthenticated(const SecurityStatus& status)
{
    if (!authenticated_ || !status.securityLockActive || tamperReportInFlight_) {
        return;
    }
    const QString fingerprint = status.securityLockReason + QLatin1Char('|') + status.integrityState;
    if (fingerprint == lastTamperReportFingerprint_) {
        return;
    }
    if (ownerMode() && ownerSecret_.isEmpty()) {
        return;
    }
    if (!ownerMode() && staffToken_.isEmpty()) {
        return;
    }

    QUrl url(apiBase());
    url.setPath(ownerMode() ? QStringLiteral("/api/admin/tamper-report") : QStringLiteral("/api/staff/tamper-report"));
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    request.setRawHeader("Accept", "application/json");
    request.setRawHeader("User-Agent", userAgent());
    request.setTransferTimeout(10'000);
    applyStrictTls(request);
    if (ownerMode()) {
        request.setRawHeader("X-Orion-Admin-Secret", ownerSecret_.toUtf8());
    } else {
        request.setRawHeader("Authorization", QByteArrayLiteral("Bearer ") + staffToken_.toUtf8());
        request.setRawHeader("X-Orion-Discord-Id", staffDiscordId_.toUtf8());
    }

    QJsonObject body;
    body.insert(QStringLiteral("event"), QStringLiteral("security_lock"));
    body.insert(QStringLiteral("detail"), status.securityLockReason);
    body.insert(QStringLiteral("integrity_state"), status.integrityState);
    body.insert(QStringLiteral("machine_id"), machineId());
    body.insert(QStringLiteral("tool_mode"), mode());
    body.insert(QStringLiteral("app_version"), appVersion());

    tamperReportInFlight_ = true;
    auto* reply = network_.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::encrypted, this, [reply]() {
        QString detail;
        if (!replyMatchesPinnedCertificate(reply, &detail)) {
            reply->setProperty("orion_pin_failed", true);
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::sslErrors, reply, [reply](const QList<QSslError>&) { reply->abort(); });
    connect(reply, &QNetworkReply::finished, this, [this, reply, fingerprint]() {
        const auto guard = qScopeGuard([reply]() { reply->deleteLater(); });
        tamperReportInFlight_ = false;
        if (reply->error() == QNetworkReply::NoError) {
            lastTamperReportFingerprint_ = fingerprint;
        }
    });
}

} // namespace orion
