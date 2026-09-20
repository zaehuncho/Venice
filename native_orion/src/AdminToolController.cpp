#include "AdminToolController.h"

#include "NetworkSecurity.h"
#include "UpdateManifest.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QProcess>
#include <QtCore/QTextStream>
#include <QtCore/QTimeZone>
#include <QtCore/QTimer>
#include <QtCore/QUrlQuery>
#include <QtCore/QUuid>
#include <QtCore/qscopeguard.h>
#include <QtGui/QClipboard>
#include <QtGui/QGuiApplication>
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

QString digitsOnly(const QString& value)
{
    QString out;
    out.reserve(value.size());
    for (const QChar ch : value) {
        if (ch.isDigit()) {
            out.append(ch);
        }
    }
    return out;
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

// Contract §1 capability matrix. Display-only: hides what a role cannot do so
// the UI is honest, but the server's require_capability() is the real gate.
bool roleCan(const QString& role, const QString& capability)
{
    if (role == QLatin1String("owner")) {
        return true;
    }
    static const QStringList adminCaps = {
        QStringLiteral("license.lookup"),
        QStringLiteral("license.create"),
        QStringLiteral("license.reset_machine"),
        QStringLiteral("license.extend"),
        QStringLiteral("license.revoke"),
        QStringLiteral("license.unrevoke"),
        QStringLiteral("license.freeze"),
        QStringLiteral("license.unfreeze"),
        QStringLiteral("license.set_plan"),
        QStringLiteral("license.transfer"),
        QStringLiteral("audit.read_own"),
    };
    static const QStringList supportCaps = {
        QStringLiteral("license.lookup"),
        QStringLiteral("license.reset_machine"),
        QStringLiteral("audit.read_own"),
    };
    if (role == QLatin1String("admin")) {
        return adminCaps.contains(capability);
    }
    if (role == QLatin1String("support")) {
        return supportCaps.contains(capability);
    }
    return false;
}

// Every capability string the panels ask about (contract §1 plus the owner-only
// extras). capabilities() turns this into the map QML binds to.
const QStringList& allCapabilities()
{
    static const QStringList caps = {
        QStringLiteral("license.lookup"),
        QStringLiteral("license.create"),
        QStringLiteral("license.reset_machine"),
        QStringLiteral("license.reset_machine force"),
        QStringLiteral("license.extend"),
        QStringLiteral("license.revoke"),
        QStringLiteral("license.unrevoke"),
        QStringLiteral("license.freeze"),
        QStringLiteral("license.unfreeze"),
        QStringLiteral("license.set_plan"),
        QStringLiteral("license.transfer"),
        QStringLiteral("license.set_reset_policy"),
        QStringLiteral("blacklist"),
        QStringLiteral("staff.manage"),
        QStringLiteral("config.write"),
        QStringLiteral("metrics"),
        QStringLiteral("audit.read_all"),
        QStringLiteral("audit.read_own"),
    };
    return caps;
}

QVariantMap objectOrEmpty(const QJsonObject& obj, const QString& key)
{
    const QJsonValue v = obj.value(key);
    return v.isObject() ? v.toObject().toVariantMap() : QVariantMap{};
}

QVariantList arrayOrEmpty(const QJsonObject& obj, const QString& key)
{
    const QJsonValue v = obj.value(key);
    return v.isArray() ? v.toArray().toVariantList() : QVariantList{};
}

QString firstString(const QJsonObject& obj, std::initializer_list<const char*> keys)
{
    for (const char* key : keys) {
        const QJsonValue v = obj.value(QLatin1String(key));
        if (v.isString() && !v.toString().isEmpty()) {
            return v.toString();
        }
    }
    return {};
}

// QVariant::toBool() has no default-value overload (QJsonValue::toBool does),
// so config flags that may be absent go through this.
bool boolOr(const QVariantMap& map, const QString& key, bool fallback)
{
    const QVariant value = map.value(key);
    return value.isValid() ? value.toBool() : fallback;
}

QJsonObject withoutOk(QJsonObject obj)
{
    obj.remove(QStringLiteral("ok"));
    return obj;
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

bool AdminToolController::ownerRoutes() const
{
    if (!authenticated_) {
        return false;
    }
    if (!ownerSecret_.isEmpty()) {
        return true;
    }
    return !staffToken_.isEmpty() && role_ == QLatin1String("owner");
}

bool AdminToolController::killSwitchEngaged() const
{
    return config_.value(QStringLiteral("global_kill")).toBool();
}

QString AdminToolController::killSwitchReason() const
{
    const QString reason = config_.value(QStringLiteral("kill_reason")).toString();
    return reason.isEmpty() ? config_.value(QStringLiteral("global_kill_reason")).toString() : reason;
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

bool AdminToolController::can(const QString& capability) const
{
    if (!authenticated_) {
        return false;
    }
    return roleCan(role_, capability);
}

QVariantMap AdminToolController::capabilities() const
{
    QVariantMap map;
    for (const QString& capability : allCapabilities()) {
        map.insert(capability, can(capability));
    }
    return map;
}

bool AdminToolController::reasonValid(const QString& reason) const
{
    const QString trimmed = reason.trimmed();
    return !trimmed.isEmpty() && trimmed.size() <= kReasonMaxChars;
}

void AdminToolController::copyToClipboard(const QString& text)
{
    if (auto* clipboard = QGuiApplication::clipboard()) {
        clipboard->setText(text);
        setStatus(QStringLiteral("Copied to clipboard."));
    }
}

QString AdminToolController::formatTs(qlonglong unixSeconds) const
{
    if (unixSeconds <= 0) {
        return QStringLiteral("-");
    }
    return QDateTime::fromSecsSinceEpoch(unixSeconds, QTimeZone::utc()).toString(QStringLiteral("yyyy-MM-dd HH:mm 'UTC'"));
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

void AdminToolController::ownerLogin(const QString& adminSecret, const QString& totpCode)
{
    appendAdminDiagnostic(rootDir_, QStringLiteral("owner_login_clicked"));
    if (!ownerMode()) {
        setStatus(QStringLiteral("Owner login is unavailable in staff build."), true);
        return;
    }
    if (!requireUsableSecurity()) {
        return;
    }
    const QString secret = normalizeSecretInput(adminSecret);
    if (looksLikeCommandInput(secret)) {
        setStatus(QStringLiteral("Paste the secret value, not the AWS command."), true);
        appendAdminDiagnostic(rootDir_, QStringLiteral("owner_login_rejected_command_input"));
        return;
    }
    if (secret.isEmpty()) {
        setStatus(QStringLiteral("Admin secret is required."), true);
        return;
    }
    ownerSecret_ = secret;
    ownerTotp_ = digitsOnly(totpCode);
    sendRequest(QStringLiteral("owner_login"), "GET", QStringLiteral("/api/admin/whoami"), {}, {}, AuthKind::Owner);
}

void AdminToolController::setTotpCode(const QString& totpCode)
{
    ownerTotp_ = digitsOnly(totpCode);
    if (!ownerTotp_.isEmpty()) {
        setStatus(QStringLiteral("TOTP code cached for this session."));
    }
}

void AdminToolController::staffEnroll(const QString& staffId, const QString& enrollKey)
{
    appendAdminDiagnostic(rootDir_, QStringLiteral("staff_enroll_clicked"));
    if (ownerMode()) {
        setStatus(QStringLiteral("Staff enrollment is unavailable in owner build."), true);
        return;
    }
    if (!requireUsableSecurity()) {
        return;
    }
    const QString id = staffId.trimmed();
    const QString key = normalizeSecretInput(enrollKey);
    if (id.isEmpty() || key.isEmpty()) {
        setStatus(QStringLiteral("Staff ID and enrollment key are required."), true);
        return;
    }
    // Contract §2: {enroll_key, staff_id, machine_id, nonce, timestamp}.
    QJsonObject body = freshnessFields();
    body.insert(QStringLiteral("staff_id"), id);
    body.insert(QStringLiteral("enroll_key"), key);
    body.insert(QStringLiteral("machine_id"), machineId());
    QJsonObject context;
    context.insert(QStringLiteral("staff_id"), id);
    sendRequest(QStringLiteral("staff_enroll"), "POST", QStringLiteral("/api/staff/enroll"), {}, body, AuthKind::None, context);
}

void AdminToolController::staffLogin(const QString& staffId)
{
    appendAdminDiagnostic(rootDir_, QStringLiteral("staff_login_clicked"));
    if (ownerMode()) {
        setStatus(QStringLiteral("Staff login is unavailable in owner build."), true);
        return;
    }
    if (!requireUsableSecurity()) {
        return;
    }
    const QString id = staffId.trimmed();
    if (id.isEmpty()) {
        setStatus(QStringLiteral("Staff ID is required."), true);
        return;
    }
    QJsonObject body = freshnessFields();
    body.insert(QStringLiteral("staff_id"), id);
    body.insert(QStringLiteral("machine_id"), machineId());
    QJsonObject context;
    context.insert(QStringLiteral("staff_id"), id);
    sendRequest(QStringLiteral("staff_login"), "POST", QStringLiteral("/api/staff/login"), {}, body, AuthKind::None, context);
}

void AdminToolController::refreshWhoami()
{
    if (!authenticated_) {
        return;
    }
    if (!staffToken_.isEmpty()) {
        sendRequest(QStringLiteral("staff_whoami"), "GET", QStringLiteral("/api/staff/whoami"), {}, {}, AuthKind::Staff);
    } else {
        sendRequest(QStringLiteral("owner_whoami"), "GET", QStringLiteral("/api/admin/whoami"), {}, {}, AuthKind::Owner);
    }
}

void AdminToolController::logout()
{
    ownerSecret_.clear();
    ownerTotp_.clear();
    staffToken_.clear();
    staffId_.clear();
    staffDiscordId_.clear();
    role_.clear();
    staffName_.clear();
    authenticated_ = false;
    resetSessionData();
    setResult(QString());
    setStatus(QStringLiteral("Signed out."));
    emit stateChanged();
    emit dataChanged();
}

void AdminToolController::resetSessionData()
{
    metrics_.clear();
    license_.clear();
    licenses_.clear();
    licenseResult_.clear();
    createdKeys_.clear();
    staffList_.clear();
    enrollSecret_.clear();
    auditRows_.clear();
    auditCursor_.clear();
    config_.clear();
    totpEnrollment_.clear();
    rotatedSecret_.clear();
    caps_.clear();
    usage_.clear();
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

void AdminToolController::refreshMetrics()
{
    if (!requireAuth(true)) {
        return;
    }
    sendRequest(QStringLiteral("metrics"), "GET", QStringLiteral("/api/admin/metrics"), {}, {}, AuthKind::Owner);
}

// ---------------------------------------------------------------------------
// Licenses
// ---------------------------------------------------------------------------

QString AdminToolController::licensePath() const
{
    return ownerRoutes() ? QStringLiteral("/api/admin/license") : QStringLiteral("/api/staff/license");
}

AdminToolController::AuthKind AdminToolController::licenseAuth() const
{
    return ownerRoutes() ? AuthKind::Owner : AuthKind::Staff;
}

void AdminToolController::licenseRequest(QJsonObject body, const QString& tag, const QJsonObject& context)
{
    sendRequest(tag, "POST", licensePath(), {}, std::move(body), licenseAuth(), context);
}

void AdminToolController::lookupLicense(const QVariantMap& query)
{
    if (!requireAuth(false)) {
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("lookup"));
    static const QStringList allowed = {
        QStringLiteral("key"), QStringLiteral("email"), QStringLiteral("discord_user_id"), QStringLiteral("machine_id"),
    };
    int provided = 0;
    for (const QString& field : allowed) {
        const QString value = query.value(field).toString().trimmed();
        if (!value.isEmpty()) {
            body.insert(field, field == QLatin1String("key") ? value.toUpper() : value);
            ++provided;
        }
    }
    if (provided == 0) {
        setStatus(QStringLiteral("Enter a key, email, Discord ID, or machine ID to look up."), true);
        return;
    }
    licenseRequest(body, QStringLiteral("license_lookup"));
}

void AdminToolController::createLicenses(const QString& plan, int days, int count, const QString& discordUserId,
                                         const QString& email, const QString& note, const QString& reason)
{
    if (!requireAuth(false) || !requireReason(reason)) {
        return;
    }
    if (plan.trimmed().isEmpty()) {
        setStatus(QStringLiteral("Plan is required."), true);
        return;
    }
    if (days < 0) {
        setStatus(QStringLiteral("Days must be 0 (lifetime) or more."), true);
        return;
    }
    if (count < 1 || count > 25) {
        setStatus(QStringLiteral("Count must be between 1 and 25 per call."), true);
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("create"));
    body.insert(QStringLiteral("reason"), reason.trimmed());
    body.insert(QStringLiteral("plan"), plan.trimmed());
    body.insert(QStringLiteral("days"), days);
    body.insert(QStringLiteral("count"), count);
    if (!discordUserId.trimmed().isEmpty()) {
        body.insert(QStringLiteral("discord_user_id"), discordUserId.trimmed());
    }
    if (!email.trimmed().isEmpty()) {
        body.insert(QStringLiteral("email"), email.trimmed());
    }
    if (!note.trimmed().isEmpty()) {
        body.insert(QStringLiteral("note"), note.trimmed().left(200));
    }
    licenseRequest(body, QStringLiteral("license_create"));
}

void AdminToolController::licenseAction(const QString& action, const QString& key, const QString& reason,
                                        const QVariantMap& extra)
{
    if (!requireAuth(false) || !requireReason(reason)) {
        return;
    }
    static const QStringList actions = {
        QStringLiteral("revoke"), QStringLiteral("unrevoke"), QStringLiteral("extend"), QStringLiteral("set_plan"),
        QStringLiteral("freeze"), QStringLiteral("unfreeze"), QStringLiteral("transfer"),
        QStringLiteral("reset_machine"), QStringLiteral("set_reset_policy"),
    };
    const QString act = action.trimmed().toLower();
    if (!actions.contains(act)) {
        setStatus(QStringLiteral("Unknown license action: %1").arg(action), true);
        return;
    }
    const QString normalizedKey = key.trimmed().toUpper();
    if (normalizedKey.isEmpty()) {
        setStatus(QStringLiteral("License key is required."), true);
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), act);
    body.insert(QStringLiteral("key"), normalizedKey);
    body.insert(QStringLiteral("reason"), reason.trimmed());
    for (auto it = extra.constBegin(); it != extra.constEnd(); ++it) {
        const QVariant& value = it.value();
        if (value.isNull() || !value.isValid()) {
            continue;
        }
        if (value.typeId() == QMetaType::QString && value.toString().trimmed().isEmpty()) {
            continue;
        }
        body.insert(it.key(), QJsonValue::fromVariant(value));
    }
    QJsonObject context;
    context.insert(QStringLiteral("action"), act);
    context.insert(QStringLiteral("key"), normalizedKey);
    licenseRequest(body, QStringLiteral("license_action"), context);
}

void AdminToolController::blacklist(const QString& kind, const QString& id, const QString& reason, bool add)
{
    if (!requireAuth(true) || !requireReason(reason)) {
        return;
    }
    const QString target = id.trimmed();
    if (target.isEmpty()) {
        setStatus(QStringLiteral("Machine ID or Discord ID is required."), true);
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), add ? QStringLiteral("blacklist") : QStringLiteral("unblacklist"));
    body.insert(QStringLiteral("reason"), reason.trimmed());
    if (kind.trimmed().toLower() == QLatin1String("discord")) {
        body.insert(QStringLiteral("discord_user_id"), target);
    } else {
        body.insert(QStringLiteral("machine_id"), target);
    }
    QJsonObject context;
    context.insert(QStringLiteral("action"), body.value(QStringLiteral("action")).toString());
    sendRequest(QStringLiteral("blacklist"), "POST", QStringLiteral("/api/admin/license"), {}, body, AuthKind::Owner, context);
}

void AdminToolController::clearCreatedKeys()
{
    createdKeys_.clear();
    emit dataChanged();
}

// ---------------------------------------------------------------------------
// Staff
// ---------------------------------------------------------------------------

void AdminToolController::refreshStaff()
{
    if (!requireAuth(true)) {
        return;
    }
    sendRequest(QStringLiteral("staff_list"), "GET", QStringLiteral("/api/admin/staff"), {}, {}, AuthKind::Owner);
}

void AdminToolController::createStaff(const QString& discordUserId, const QString& role, const QVariantMap& caps,
                                      const QString& reason)
{
    if (!requireAuth(true) || !requireReason(reason)) {
        return;
    }
    const QString discord = discordUserId.trimmed();
    const QString normalizedRole = role.trimmed().toLower();
    if (discord.isEmpty()) {
        setStatus(QStringLiteral("Discord user ID is required."), true);
        return;
    }
    static const QStringList roles = { QStringLiteral("owner"), QStringLiteral("admin"), QStringLiteral("support") };
    if (!roles.contains(normalizedRole)) {
        setStatus(QStringLiteral("Role must be owner, admin, or support."), true);
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("create"));
    body.insert(QStringLiteral("discord_user_id"), discord);
    body.insert(QStringLiteral("role"), normalizedRole);
    body.insert(QStringLiteral("reason"), reason.trimmed());
    if (!caps.isEmpty()) {
        body.insert(QStringLiteral("caps"), QJsonObject::fromVariantMap(caps));
    }
    sendRequest(QStringLiteral("staff_create"), "POST", QStringLiteral("/api/admin/staff"), {}, body, AuthKind::Owner);
}

void AdminToolController::staffAction(const QString& action, const QString& staffId, const QString& reason,
                                      const QVariantMap& extra)
{
    if (!requireAuth(true) || !requireReason(reason)) {
        return;
    }
    static const QStringList actions = {
        QStringLiteral("disable"), QStringLiteral("enable"), QStringLiteral("set_role"), QStringLiteral("set_caps"),
        QStringLiteral("reset_machine"), QStringLiteral("reissue_enrollment"),
    };
    const QString act = action.trimmed().toLower();
    if (!actions.contains(act)) {
        setStatus(QStringLiteral("Unknown staff action: %1").arg(action), true);
        return;
    }
    const QString id = staffId.trimmed();
    if (id.isEmpty()) {
        setStatus(QStringLiteral("Staff ID is required."), true);
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), act);
    body.insert(QStringLiteral("staff_id"), id);
    body.insert(QStringLiteral("reason"), reason.trimmed());
    for (auto it = extra.constBegin(); it != extra.constEnd(); ++it) {
        body.insert(it.key(), QJsonValue::fromVariant(it.value()));
    }
    QJsonObject context;
    context.insert(QStringLiteral("action"), act);
    context.insert(QStringLiteral("staff_id"), id);
    sendRequest(QStringLiteral("staff_action"), "POST", QStringLiteral("/api/admin/staff"), {}, body, AuthKind::Owner, context);
}

void AdminToolController::clearEnrollSecret()
{
    enrollSecret_.clear();
    emit dataChanged();
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

void AdminToolController::fetchAudit(const QVariantMap& filters, const QString& cursor, int limit)
{
    if (!requireAuth(false)) {
        return;
    }
    QJsonObject query;
    const bool ownerAudit = ownerRoutes();
    if (ownerAudit) {
        static const QStringList keys = {
            QStringLiteral("since"), QStringLiteral("until"), QStringLiteral("actor"),
            QStringLiteral("action"), QStringLiteral("target"),
        };
        for (const QString& key : keys) {
            const QString value = filters.value(key).toString().trimmed();
            if (!value.isEmpty()) {
                query.insert(key, value);
            }
        }
        query.insert(QStringLiteral("limit"), QString::number(qBound(1, limit, 200)));
    }
    if (!cursor.trimmed().isEmpty()) {
        query.insert(QStringLiteral("cursor"), cursor.trimmed());
    }
    QJsonObject context;
    context.insert(QStringLiteral("append"), !cursor.trimmed().isEmpty());
    sendRequest(QStringLiteral("audit"), "GET",
                ownerAudit ? QStringLiteral("/api/admin/audit") : QStringLiteral("/api/staff/audit"),
                query, {}, ownerAudit ? AuthKind::Owner : AuthKind::Staff, context);
}

void AdminToolController::clearAudit()
{
    auditRows_.clear();
    auditCursor_.clear();
    emit dataChanged();
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

void AdminToolController::refreshConfig()
{
    if (!requireAuth(true)) {
        return;
    }
    sendRequest(QStringLiteral("config"), "GET", QStringLiteral("/api/admin/config"), {}, {}, AuthKind::Owner);
}

void AdminToolController::updateConfig(const QVariantMap& patch, const QString& reason)
{
    if (!requireAuth(true) || !requireReason(reason)) {
        return;
    }
    if (patch.isEmpty()) {
        setStatus(QStringLiteral("Nothing to update."), true);
        return;
    }
    QJsonObject body = QJsonObject::fromVariantMap(patch);
    body.remove(QStringLiteral("action"));
    body.insert(QStringLiteral("reason"), reason.trimmed());
    QJsonObject context;
    context.insert(QStringLiteral("keys"), QJsonArray::fromStringList(patch.keys()));
    sendRequest(QStringLiteral("config_update"), "POST", QStringLiteral("/api/admin/config"), {}, body, AuthKind::Owner, context);
}

void AdminToolController::setGlobalKill(bool engaged, const QString& reason)
{
    QVariantMap patch;
    patch.insert(QStringLiteral("global_kill"), engaged);
    updateConfig(patch, reason);
}

void AdminToolController::totpEnroll(const QString& reason)
{
    if (!requireAuth(true) || !requireReason(reason)) {
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("totp_enroll"));
    body.insert(QStringLiteral("reason"), reason.trimmed());
    sendRequest(QStringLiteral("totp_enroll"), "POST", QStringLiteral("/api/admin/config"), {}, body, AuthKind::Owner);
}

void AdminToolController::totpConfirm(const QString& code, const QString& reason)
{
    if (!requireAuth(true) || !requireReason(reason)) {
        return;
    }
    const QString digits = digitsOnly(code);
    if (digits.size() < 6) {
        setStatus(QStringLiteral("Enter the 6-digit code from the authenticator."), true);
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("totp_confirm"));
    body.insert(QStringLiteral("code"), digits);
    body.insert(QStringLiteral("reason"), reason.trimmed());
    QJsonObject context;
    context.insert(QStringLiteral("code"), digits);
    sendRequest(QStringLiteral("totp_confirm"), "POST", QStringLiteral("/api/admin/config"), {}, body, AuthKind::Owner, context);
}

void AdminToolController::rotateAdminSecret(const QString& reason)
{
    if (!requireAuth(true) || !requireReason(reason)) {
        return;
    }
    QJsonObject body;
    body.insert(QStringLiteral("action"), QStringLiteral("rotate_admin_secret"));
    body.insert(QStringLiteral("reason"), reason.trimmed());
    sendRequest(QStringLiteral("rotate_secret"), "POST", QStringLiteral("/api/admin/config"), {}, body, AuthKind::Owner);
}

void AdminToolController::clearTotpEnrollment()
{
    totpEnrollment_.clear();
    emit dataChanged();
}

void AdminToolController::clearRotatedSecret()
{
    rotatedSecret_.clear();
    emit dataChanged();
}

// ---------------------------------------------------------------------------
// Tool housekeeping
// ---------------------------------------------------------------------------

void AdminToolController::checkUpdate()
{
    updateState_ = QStringLiteral("Checking...");
    emit stateChanged();
    licenseClient_.checkUpdate(appVersion(), QStringLiteral("stable"));
}

void AdminToolController::startUpdate()
{
    if (!updateAvailable_) {
        setStatus(QStringLiteral("No update is available."), true);
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

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

void AdminToolController::beginRequest()
{
    ++inflight_;
    if (inflight_ == 1) {
        emit stateChanged();
    }
}

void AdminToolController::endRequest()
{
    if (inflight_ > 0) {
        --inflight_;
    }
    if (inflight_ == 0) {
        emit stateChanged();
    }
}

void AdminToolController::setStatus(QString value, bool isError)
{
    statusMessage_ = std::move(value);
    statusIsError_ = isError;
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
        setStatus(QStringLiteral("Security lock active. Privileged tool refused."), true);
        return false;
    }
    return true;
}

bool AdminToolController::requireAuth(bool ownerRoutesNeeded)
{
    if (!authenticated_) {
        setStatus(QStringLiteral("Authentication is required."), true);
        return false;
    }
    if (ownerRoutesNeeded && !ownerRoutes()) {
        setStatus(QStringLiteral("Owner authentication is required."), true);
        return false;
    }
    return true;
}

bool AdminToolController::requireReason(const QString& reason)
{
    if (reason.trimmed().isEmpty()) {
        setStatus(QStringLiteral("Reason is required."), true);
        return false;
    }
    if (reason.trimmed().size() > kReasonMaxChars) {
        setStatus(QStringLiteral("Reason must be %1 characters or fewer.").arg(kReasonMaxChars), true);
        return false;
    }
    return true;
}

void AdminToolController::applyAuth(QNetworkRequest& request, AuthKind authKind) const
{
    const auto staffHeaders = [this, &request]() {
        request.setRawHeader("Authorization", QByteArrayLiteral("Bearer ") + staffToken_.toUtf8());
        // require_staff binds the token to the machine it was issued to; the
        // header is mandatory for bound tokens (contract §1).
        request.setRawHeader("X-Machine-Id", machineId().toUtf8());
        if (!staffDiscordId_.isEmpty()) {
            request.setRawHeader("X-Orion-Discord-Id", staffDiscordId_.toUtf8());
        }
    };
    switch (authKind) {
    case AuthKind::None:
        break;
    case AuthKind::Owner:
        if (!ownerSecret_.isEmpty()) {
            request.setRawHeader("X-Orion-Admin-Secret", ownerSecret_.toUtf8());
            if (!ownerTotp_.isEmpty()) {
                request.setRawHeader("X-Orion-Admin-TOTP", ownerTotp_.toUtf8());
            }
        } else if (!staffToken_.isEmpty()) {
            staffHeaders();
        }
        break;
    case AuthKind::Staff:
        staffHeaders();
        break;
    }
}

void AdminToolController::sendRequest(const QString& tag, const QByteArray& method, const QString& path,
                                      const QJsonObject& query, const QJsonObject& body, AuthKind authKind,
                                      const QJsonObject& context)
{
    if (!requireUsableSecurity()) {
        return;
    }
    beginRequest();
    QUrl url(apiBase());
    url.setPath(path);
    if (!query.isEmpty()) {
        QUrlQuery q;
        for (auto it = query.constBegin(); it != query.constEnd(); ++it) {
            q.addQueryItem(it.key(), it.value().toString());
        }
        url.setQuery(q);
    }
    QNetworkRequest request(url);
    request.setRawHeader("Accept", "application/json");
    request.setRawHeader("User-Agent", userAgent());
    request.setTransferTimeout(20'000);
    applyStrictTls(request);
    applyAuth(request, authKind);

    QNetworkReply* reply = nullptr;
    if (method == "GET") {
        reply = network_.get(request);
    } else {
        request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
        reply = network_.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
    }

    PendingRequest pending;
    pending.tag = tag;
    pending.path = path;
    pending.body = body;
    pending.context = context;

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
    connect(reply, &QNetworkReply::finished, this, [this, reply, pending]() {
        const auto guard = qScopeGuard([reply]() { reply->deleteLater(); });
        const bool ok = reply->error() == QNetworkReply::NoError;
        const int httpStatus = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        appendAdminDiagnostic(rootDir_,
                              QStringLiteral("http_finished"),
                              QStringLiteral("tag=%1 path=%2 status=%3 net_error=%4")
                                  .arg(pending.tag, pending.path)
                                  .arg(httpStatus)
                                  .arg(static_cast<int>(reply->error())));
        if (reply->property("orion_pin_failed").toBool()) {
            endRequest();
            setStatus(QStringLiteral("Pinned server certificate did not match."), true);
            emit requestFinished(pending.tag, false, {});
            return;
        }
        handleResponse(pending, reply->readAll(), httpStatus, ok);
    });
}

void AdminToolController::handleResponse(const PendingRequest& pending, const QByteArray& payload, int httpStatus, bool ok)
{
    endRequest();
    QJsonParseError parseError;
    const auto doc = QJsonDocument::fromJson(payload, &parseError);
    const QJsonObject obj = doc.isObject() ? doc.object() : QJsonObject{};
    const bool parsed = parseError.error == QJsonParseError::NoError && doc.isObject();
    const bool serverOk = parsed && obj.value(QStringLiteral("ok")).toBool(false);

    if (!serverOk) {
        const QString code = parsed ? obj.value(QStringLiteral("error")).toString() : QString();
        QString message = parsed ? obj.value(QStringLiteral("message")).toString() : QString();
        const bool loginTag = pending.tag == QLatin1String("owner_login")
            || pending.tag == QLatin1String("staff_login")
            || pending.tag == QLatin1String("staff_enroll");

        if (code == QLatin1String("totp_required") || code == QLatin1String("invalid_totp")
            || code == QLatin1String("totp_invalid") || code == QLatin1String("bad_totp")) {
            totpRequired_ = true;
            if (message.isEmpty()) {
                message = code == QLatin1String("totp_required")
                    ? QStringLiteral("This owner account requires a TOTP code. Enter the current code and retry.")
                    : QStringLiteral("TOTP code rejected. Enter the current 6-digit code and retry.");
            }
        } else if (code == QLatin1String("token_expired") || code == QLatin1String("machine_mismatch")
                   || code == QLatin1String("staff_disabled") || code == QLatin1String("invalid_token")) {
            if (message.isEmpty()) {
                message = QStringLiteral("Staff session rejected (%1). Log in again.").arg(code);
            }
            if (authenticated_ && !loginTag && !staffToken_.isEmpty()) {
                staffToken_.clear();
                authenticated_ = false;
                resetSessionData();
                emit dataChanged();
            }
        } else if (code == QLatin1String("ip_not_allowed")) {
            if (message.isEmpty()) {
                message = QStringLiteral("This IP is not on the owner allowlist.");
            }
        } else if (code == QLatin1String("rate_limited")) {
            if (message.isEmpty()) {
                message = QStringLiteral("Rate limited by the server. Wait a moment and retry.");
            }
        } else if (code == QLatin1String("version_blocked")) {
            if (message.isEmpty()) {
                message = QStringLiteral("This tool version is blocked. Update required.");
            }
        } else if (message.isEmpty()) {
            if (!code.isEmpty()) {
                message = QStringLiteral("Request rejected: %1").arg(code);
            } else if (pending.tag == QLatin1String("owner_login") && (httpStatus == 401 || httpStatus == 403)) {
                message = QStringLiteral("Owner secret rejected by server.");
            } else if (httpStatus == 0) {
                message = QStringLiteral("Request failed before reaching the server.");
            } else if (httpStatus == 404) {
                message = QStringLiteral("Endpoint not deployed yet (%1).").arg(pending.path);
            } else {
                message = QStringLiteral("Request failed (%1).").arg(httpStatus);
            }
        }
        if (pending.tag == QLatin1String("owner_login")) {
            // A rejected login never leaves the typed secret in memory; the
            // form resubmits it (plus the TOTP code) on the next attempt.
            ownerSecret_.clear();
            ownerTotp_.clear();
        }
        setStatus(message, true);
        if (parsed) {
            setResult(QString::fromUtf8(QJsonDocument(obj).toJson(QJsonDocument::Indented)));
        }
        emit requestFinished(pending.tag, false, obj.toVariantMap());
        Q_UNUSED(ok);
        return;
    }

    handleSuccess(pending, obj);
    setResult(QString::fromUtf8(QJsonDocument(obj).toJson(QJsonDocument::Indented)));
    emit dataChanged();
    emit requestFinished(pending.tag, true, obj.toVariantMap());
}

void AdminToolController::handleSuccess(const PendingRequest& pending, const QJsonObject& obj)
{
    const QString& tag = pending.tag;

    if (tag == QLatin1String("owner_login") || tag == QLatin1String("owner_whoami")) {
        authenticated_ = true;
        role_ = QStringLiteral("owner");
        staffName_ = QStringLiteral("Owner");
        totpRequired_ = obj.value(QStringLiteral("totp_required")).toBool(totpRequired_);
        setStatus(tag == QLatin1String("owner_login") ? QStringLiteral("Owner authenticated.") : QStringLiteral("Session verified."));
        return;
    }

    if (tag == QLatin1String("staff_enroll") || tag == QLatin1String("staff_login")) {
        authenticated_ = true;
        staffToken_ = obj.value(QStringLiteral("token")).toString();
        const QJsonObject staff = obj.value(QStringLiteral("staff")).toObject();
        role_ = firstString(staff, { "role" });
        if (role_.isEmpty()) {
            role_ = obj.value(QStringLiteral("role")).toString(QStringLiteral("support"));
        }
        if (role_ == QLatin1String("staff")) {
            role_ = QStringLiteral("support"); // legacy rows minted before roles existed
        }
        staffId_ = firstString(staff, { "staff_id" });
        if (staffId_.isEmpty()) {
            staffId_ = obj.value(QStringLiteral("staff_id")).toString(pending.context.value(QStringLiteral("staff_id")).toString());
        }
        staffDiscordId_ = firstString(staff, { "discord_user_id" });
        if (staffDiscordId_.isEmpty()) {
            staffDiscordId_ = obj.value(QStringLiteral("discord_user_id")).toString();
        }
        staffName_ = firstString(staff, { "display_name" });
        if (staffName_.isEmpty()) {
            staffName_ = obj.value(QStringLiteral("display_name")).toString(staffId_);
        }
        caps_ = staff.contains(QStringLiteral("caps")) ? objectOrEmpty(staff, QStringLiteral("caps")) : objectOrEmpty(obj, QStringLiteral("caps"));
        usage_ = staff.contains(QStringLiteral("usage")) ? objectOrEmpty(staff, QStringLiteral("usage")) : objectOrEmpty(obj, QStringLiteral("usage"));
        setStatus(tag == QLatin1String("staff_enroll") ? QStringLiteral("Staff enrolled on this machine.") : QStringLiteral("Staff authenticated."));
        // Pull caps/usage (contract §7: staff panel shows own caps and usage).
        sendRequest(QStringLiteral("staff_whoami"), "GET", QStringLiteral("/api/staff/whoami"), {}, {}, AuthKind::Staff);
        return;
    }

    if (tag == QLatin1String("staff_whoami")) {
        const QJsonObject staff = obj.contains(QStringLiteral("staff")) ? obj.value(QStringLiteral("staff")).toObject() : obj;
        const QString role = staff.value(QStringLiteral("role")).toString();
        if (!role.isEmpty()) {
            role_ = role == QLatin1String("staff") ? QStringLiteral("support") : role;
        }
        const QString name = staff.value(QStringLiteral("display_name")).toString();
        if (!name.isEmpty()) {
            staffName_ = name;
        }
        const QString id = staff.value(QStringLiteral("staff_id")).toString();
        if (!id.isEmpty()) {
            staffId_ = id;
        }
        const QString discord = staff.value(QStringLiteral("discord_user_id")).toString();
        if (!discord.isEmpty()) {
            staffDiscordId_ = discord;
        }
        caps_ = objectOrEmpty(staff, QStringLiteral("caps"));
        usage_ = objectOrEmpty(staff, QStringLiteral("usage"));
        setStatus(QStringLiteral("Session verified."));
        return;
    }

    if (tag == QLatin1String("metrics")) {
        metrics_ = obj.contains(QStringLiteral("metrics")) ? objectOrEmpty(obj, QStringLiteral("metrics")) : withoutOk(obj).toVariantMap();
        setStatus(QStringLiteral("Metrics refreshed."));
        return;
    }

    if (tag == QLatin1String("license_lookup")) {
        licenseResult_ = withoutOk(obj).toVariantMap();
        licenses_ = arrayOrEmpty(obj, QStringLiteral("licenses"));
        if (obj.value(QStringLiteral("license")).isObject()) {
            license_ = objectOrEmpty(obj, QStringLiteral("license"));
        } else if (licenses_.size() == 1) {
            license_ = licenses_.first().toMap();
        } else {
            license_.clear();
        }
        if (license_.isEmpty() && licenses_.isEmpty() && obj.value(QStringLiteral("found")).toBool(true) == false) {
            setStatus(QStringLiteral("No license matched."), true);
        } else {
            const int n = licenses_.isEmpty() ? (license_.isEmpty() ? 0 : 1) : static_cast<int>(licenses_.size());
            setStatus(n == 1 ? QStringLiteral("License found.") : QStringLiteral("%1 licenses matched.").arg(n));
        }
        return;
    }

    if (tag == QLatin1String("license_create")) {
        createdKeys_ = arrayOrEmpty(obj, QStringLiteral("keys"));
        if (createdKeys_.isEmpty() && obj.contains(QStringLiteral("license_key"))) {
            createdKeys_.append(obj.value(QStringLiteral("license_key")).toString());
        }
        setStatus(QStringLiteral("%1 license(s) created. Copy the keys now; they are not shown in full again.")
                      .arg(createdKeys_.size()));
        return;
    }

    if (tag == QLatin1String("license_action")) {
        const QString action = pending.context.value(QStringLiteral("action")).toString();
        const QString key = pending.context.value(QStringLiteral("key")).toString();
        if (obj.value(QStringLiteral("license")).isObject()) {
            license_ = objectOrEmpty(obj, QStringLiteral("license"));
            licenseResult_ = withoutOk(obj).toVariantMap();
        } else if (!key.isEmpty()) {
            QJsonObject body;
            body.insert(QStringLiteral("action"), QStringLiteral("lookup"));
            body.insert(QStringLiteral("key"), key);
            licenseRequest(body, QStringLiteral("license_lookup"));
        }
        setStatus(QStringLiteral("License %1 applied to ...%2.").arg(action, key.right(4)));
        return;
    }

    if (tag == QLatin1String("blacklist")) {
        setStatus(QStringLiteral("%1 applied.").arg(pending.context.value(QStringLiteral("action")).toString()));
        return;
    }

    if (tag == QLatin1String("staff_list")) {
        staffList_ = arrayOrEmpty(obj, QStringLiteral("staff"));
        setStatus(QStringLiteral("%1 staff member(s).").arg(staffList_.size()));
        return;
    }

    if (tag == QLatin1String("staff_create")) {
        const QJsonObject staff = obj.value(QStringLiteral("staff")).isObject() ? obj.value(QStringLiteral("staff")).toObject() : obj;
        QVariantMap secret;
        secret.insert(QStringLiteral("staff_id"), firstString(staff, { "staff_id" }).isEmpty() ? firstString(obj, { "staff_id" }) : firstString(staff, { "staff_id" }));
        secret.insert(QStringLiteral("enroll_key"), firstString(obj, { "enroll_key", "enrollment_key" }).isEmpty()
                          ? firstString(staff, { "enroll_key", "enrollment_key" })
                          : firstString(obj, { "enroll_key", "enrollment_key" }));
        secret.insert(QStringLiteral("role"), firstString(staff, { "role" }).isEmpty() ? pending.body.value(QStringLiteral("role")).toString() : firstString(staff, { "role" }));
        secret.insert(QStringLiteral("discord_user_id"), pending.body.value(QStringLiteral("discord_user_id")).toString());
        enrollSecret_ = secret;
        setStatus(QStringLiteral("Staff created. Hand the enrollment key to the staff member now; it is shown once."));
        sendRequest(QStringLiteral("staff_list"), "GET", QStringLiteral("/api/admin/staff"), {}, {}, AuthKind::Owner);
        return;
    }

    if (tag == QLatin1String("staff_action")) {
        const QString action = pending.context.value(QStringLiteral("action")).toString();
        const QString id = pending.context.value(QStringLiteral("staff_id")).toString();
        if (action == QLatin1String("reissue_enrollment")) {
            QVariantMap secret;
            secret.insert(QStringLiteral("staff_id"), id);
            secret.insert(QStringLiteral("enroll_key"), firstString(obj, { "enroll_key", "enrollment_key" }));
            secret.insert(QStringLiteral("role"), QString());
            secret.insert(QStringLiteral("discord_user_id"), QString());
            enrollSecret_ = secret;
            setStatus(QStringLiteral("Enrollment key reissued for %1. It is shown once.").arg(id));
        } else {
            setStatus(QStringLiteral("Staff %1 applied to %2.").arg(action, id));
        }
        sendRequest(QStringLiteral("staff_list"), "GET", QStringLiteral("/api/admin/staff"), {}, {}, AuthKind::Owner);
        return;
    }

    if (tag == QLatin1String("audit")) {
        QVariantList rows = arrayOrEmpty(obj, QStringLiteral("audit"));
        if (rows.isEmpty()) {
            rows = arrayOrEmpty(obj, QStringLiteral("rows"));
        }
        if (rows.isEmpty()) {
            rows = arrayOrEmpty(obj, QStringLiteral("events"));
        }
        const bool append = pending.context.value(QStringLiteral("append")).toBool();
        if (append) {
            auditRows_ += rows;
        } else {
            auditRows_ = rows;
        }
        auditCursor_ = firstString(obj, { "next_cursor", "cursor" });
        setStatus(QStringLiteral("%1 audit row(s) loaded%2.")
                      .arg(auditRows_.size())
                      .arg(auditCursor_.isEmpty() ? QString() : QStringLiteral(" (more available)")));
        return;
    }

    if (tag == QLatin1String("config")) {
        config_ = obj.value(QStringLiteral("config")).isObject() ? objectOrEmpty(obj, QStringLiteral("config")) : withoutOk(obj).toVariantMap();
        totpRequired_ = boolOr(config_, QStringLiteral("owner_totp_required"), totpRequired_);
        setStatus(QStringLiteral("Config loaded."));
        return;
    }

    if (tag == QLatin1String("config_update")) {
        if (obj.value(QStringLiteral("config")).isObject()) {
            config_ = objectOrEmpty(obj, QStringLiteral("config"));
        } else {
            // Merge the patch locally, then confirm with a fresh read.
            const QJsonObject body = pending.body;
            for (auto it = body.constBegin(); it != body.constEnd(); ++it) {
                if (it.key() != QLatin1String("reason")) {
                    config_.insert(it.key(), it.value().toVariant());
                }
            }
            sendRequest(QStringLiteral("config"), "GET", QStringLiteral("/api/admin/config"), {}, {}, AuthKind::Owner);
        }
        totpRequired_ = boolOr(config_, QStringLiteral("owner_totp_required"), totpRequired_);
        const QJsonArray keys = pending.context.value(QStringLiteral("keys")).toArray();
        QStringList names;
        for (const auto& k : keys) {
            names.append(k.toString());
        }
        setStatus(QStringLiteral("Config updated: %1.").arg(names.join(QStringLiteral(", "))));
        return;
    }

    if (tag == QLatin1String("totp_enroll")) {
        const QJsonObject totp = obj.value(QStringLiteral("totp")).isObject() ? obj.value(QStringLiteral("totp")).toObject() : obj;
        QVariantMap enrollment;
        enrollment.insert(QStringLiteral("otpauth_uri"), firstString(totp, { "otpauth_uri", "otpauth", "uri" }));
        enrollment.insert(QStringLiteral("secret"), firstString(totp, { "secret", "base32_secret" }));
        totpEnrollment_ = enrollment;
        setStatus(QStringLiteral("TOTP secret issued. Add it to the authenticator, then confirm with a code."));
        return;
    }

    if (tag == QLatin1String("totp_confirm")) {
        totpRequired_ = true;
        ownerTotp_ = pending.context.value(QStringLiteral("code")).toString();
        totpEnrollment_.clear();
        config_.insert(QStringLiteral("owner_totp_required"), true);
        setStatus(QStringLiteral("TOTP confirmed. Owner secret requests now require a code."));
        sendRequest(QStringLiteral("config"), "GET", QStringLiteral("/api/admin/config"), {}, {}, AuthKind::Owner);
        return;
    }

    if (tag == QLatin1String("rotate_secret")) {
        rotatedSecret_ = firstString(obj, { "admin_secret", "new_secret", "secret" });
        if (!rotatedSecret_.isEmpty() && !ownerSecret_.isEmpty()) {
            // The old secret is dead the moment SSM is rewritten; keep this
            // session alive with the new one.
            ownerSecret_ = rotatedSecret_;
        }
        setStatus(QStringLiteral("Admin secret rotated. Store the new secret now; it is shown once."));
        return;
    }

    setStatus(obj.value(QStringLiteral("message")).toString(QStringLiteral("Request completed.")));
}

QJsonObject AdminToolController::freshnessFields() const
{
    const std::time_t unixNow = std::time(nullptr);
    QJsonObject obj;
    // Contract §2 / Lambda handle_staff_enroll: `nonce` + `timestamp`.
    obj.insert(QStringLiteral("nonce"), QUuid::createUuid().toString(QUuid::WithoutBraces));
    obj.insert(QStringLiteral("timestamp"),
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
        setStatus(QStringLiteral("Update refused from build tree."), true);
        return;
    }
    const QString installDir = QCoreApplication::applicationDirPath();
    const QString updaterPath = installDir + QStringLiteral("/OrionUpdater.exe");
    if (!QFileInfo::exists(updaterPath)) {
        setStatus(QStringLiteral("Updater missing."), true);
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
        setStatus(QStringLiteral("Updater failed to start."), true);
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
    const bool useOwner = !ownerSecret_.isEmpty();
    if (!useOwner && staffToken_.isEmpty()) {
        return;
    }

    QUrl url(apiBase());
    url.setPath(useOwner ? QStringLiteral("/api/admin/tamper-report") : QStringLiteral("/api/staff/tamper-report"));
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    request.setRawHeader("Accept", "application/json");
    request.setRawHeader("User-Agent", userAgent());
    request.setTransferTimeout(10'000);
    applyStrictTls(request);
    applyAuth(request, useOwner ? AuthKind::Owner : AuthKind::Staff);

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
