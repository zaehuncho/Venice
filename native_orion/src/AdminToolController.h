#pragma once

#include "LicenseClient.h"
#include "SecurityManager.h"

#include <QtCore/QJsonObject>
#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QVariantList>
#include <QtCore/QVariantMap>
#include <QtNetwork/QNetworkAccessManager>

namespace orion {

// [rc1 RT-CRIT-01 client, P-F] Owner step-up policy. Pure functions (QString /
// QVariant only) so the policy is testable without a network or a controller.
// Mirrors backend/lambda_function.py owner_step_up() callers:
//   * config actions totp_enroll / totp_confirm / totp_disable / rotate_admin_secret
//   * config set of owner_totp_required, owner_ip_allowlist, alerts, or RELEASING
//     global_kill (engaging the kill stays one step: the safe direction)
//   * staff create with role owner; any staff action on an owner row; set_role -> owner
// The fresh code travels ONLY in the X-Orion-Admin-TOTP header of that one request.
namespace admin_step_up {

inline constexpr int kCodeDigits = 6;

inline bool configActionNeedsStepUp(const QString& action)
{
    const QString a = action.trimmed().toLower();
    return a == QLatin1String("totp_enroll") || a == QLatin1String("totp_confirm")
        || a == QLatin1String("totp_disable") || a == QLatin1String("rotate_admin_secret");
}

inline bool killValueEnabled(const QVariant& value)
{
    if (value.typeId() == QMetaType::QVariantMap) {
        return value.toMap().value(QStringLiteral("enabled")).toBool();
    }
    return value.toBool();
}

inline bool configPatchNeedsStepUp(const QVariantMap& patch)
{
    for (auto it = patch.constBegin(); it != patch.constEnd(); ++it) {
        const QString& key = it.key();
        if (key == QLatin1String("owner_totp_required") || key == QLatin1String("owner_ip_allowlist")
            || key == QLatin1String("alerts")) {
            return true;
        }
        if (key == QLatin1String("global_kill") && !killValueEnabled(it.value())) {
            return true;
        }
    }
    return false;
}

inline bool staffCreateNeedsStepUp(const QString& role)
{
    return role.trimmed().toLower() == QLatin1String("owner");
}

// targetRole: the row's current role from the staff list; empty = unknown, which
// is treated as "may be an owner row" (the server would demand the code anyway).
inline bool staffActionNeedsStepUp(const QString& action, const QString& targetRole, const QVariantMap& extra)
{
    const QString role = targetRole.trimmed().toLower();
    if (role.isEmpty() || role == QLatin1String("owner")) {
        return true;
    }
    return action.trimmed().toLower() == QLatin1String("set_role")
        && extra.value(QStringLiteral("role")).toString().trimmed().toLower() == QLatin1String("owner");
}

// configTotpState: 1 = config says owner_totp_required true, 0 = config says false,
// -1 = config not loaded. Break-glass: the admin secret is itself the possession
// factor, so a code is only asked for when TOTP is required. Owner-role staff
// bearer: the server never lets a bearer act without a code, so ask unless the
// config positively says TOTP is off (then the server answers step_up_required
// and asking for a code would be pointless).
inline bool shouldPromptForCode(bool breakGlass, bool totpRequiredFlag, int configTotpState)
{
    if (breakGlass) {
        return totpRequiredFlag || configTotpState == 1;
    }
    return !(configTotpState == 0 && !totpRequiredFlag);
}

inline QString sanitizeCode(const QString& value)
{
    QString out;
    for (const QChar ch : value) {
        if (ch.isDigit()) {
            out.append(ch);
        }
    }
    return out;
}

inline bool codeWellFormed(const QString& digits)
{
    return digits.size() == kCodeDigits;
}

// Codes where a fresh code can fix it: the prompt re-opens for the same action.
inline bool codeRetryable(const QString& error)
{
    return error == QLatin1String("invalid_totp") || error == QLatin1String("totp_replayed")
        || error == QLatin1String("totp_required");
}

inline QString replayedOrWrongMessage()
{
    return QStringLiteral("That code was already used or is wrong \u2014 wait for the next code.");
}

// Plain message for a step-up / owner-login error code, or empty when the code
// is not a step-up code.
inline QString plainMessage(const QString& error)
{
    if (error == QLatin1String("invalid_totp") || error == QLatin1String("totp_replayed")
        || error == QLatin1String("totp_invalid") || error == QLatin1String("bad_totp")) {
        return replayedOrWrongMessage();
    }
    if (error == QLatin1String("totp_required")) {
        return QStringLiteral("This needs a fresh owner code. Enter the current 6-digit code.");
    }
    if (error == QLatin1String("step_up_required")) {
        return QStringLiteral("This action needs owner TOTP. Turn TOTP on from the break-glass owner console first.");
    }
    if (error == QLatin1String("step_up_unavailable")) {
        return QStringLiteral("The code check is unavailable right now. Nothing changed; try again shortly.");
    }
    if (error == QLatin1String("totp_not_provisioned")) {
        return QStringLiteral("Owner TOTP is required but not set up on the server. Use the owner recovery runbook.");
    }
    if (error == QLatin1String("config_unavailable")) {
        return QStringLiteral("Security settings could not be read. Nothing changed; try again shortly.");
    }
    if (error == QLatin1String("audit_unavailable")) {
        return QStringLiteral("The audit log is unavailable, so nothing was changed. Try again shortly.");
    }
    return {};
}

} // namespace admin_step_up

// Client controller for OrionOwner.exe / OrionStaff.exe. Implements the client
// side of docs/ADMIN_PANEL_V2_CONTRACT.md §7 against the endpoints in §2-§6.
//
// Auth model:
//   * Owner (break-glass): X-Orion-Admin-Secret (+ X-Orion-Admin-TOTP when the
//     owner has enrolled TOTP). Both are prompted and held in memory only.
//   * Staff: Bearer token + X-Machine-Id (the machine the token was issued to).
//     A staff row with role=owner may use the owner routes with the same token.
//   * Owner step-up (rc1 RT-CRIT-01): owner-sensitive actions (see admin_step_up)
//     need a FRESH single-use owner TOTP code. The controller parks the action,
//     QML asks for the code (stepUpPending), submitStepUp() sends it once in
//     X-Orion-Admin-TOTP and drops it. It is never stored, logged or put in a
//     request body. An owner-role staff login sends body `totp_code` only when
//     the owner typed one.
// The capability matrix in can() is DISPLAY-ONLY; the server is the real gate.
class AdminToolController final : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString mode READ mode CONSTANT)
    Q_PROPERTY(bool ownerMode READ ownerMode CONSTANT)
    Q_PROPERTY(bool authenticated READ authenticated NOTIFY stateChanged)
    Q_PROPERTY(bool busy READ busy NOTIFY stateChanged)
    Q_PROPERTY(QString role READ role NOTIFY stateChanged)
    Q_PROPERTY(QString staffName READ staffName NOTIFY stateChanged)
    Q_PROPERTY(QString staffId READ staffId NOTIFY stateChanged)
    Q_PROPERTY(bool ownerRoutes READ ownerRoutes NOTIFY stateChanged)
    Q_PROPERTY(bool totpRequired READ totpRequired NOTIFY stateChanged)
    Q_PROPERTY(QString statusMessage READ statusMessage NOTIFY stateChanged)
    Q_PROPERTY(bool statusIsError READ statusIsError NOTIFY stateChanged)
    Q_PROPERTY(QString resultText READ resultText NOTIFY stateChanged)
    Q_PROPERTY(QString securityState READ securityState NOTIFY stateChanged)
    Q_PROPERTY(bool securityLockActive READ securityLockActive NOTIFY stateChanged)
    Q_PROPERTY(QString updateState READ updateState NOTIFY stateChanged)
    Q_PROPERTY(bool updateAvailable READ updateAvailable NOTIFY stateChanged)
    Q_PROPERTY(QString latestVersion READ latestVersion NOTIFY stateChanged)
    Q_PROPERTY(bool killSwitchEngaged READ killSwitchEngaged NOTIFY dataChanged)
    Q_PROPERTY(QString killSwitchReason READ killSwitchReason NOTIFY dataChanged)
    Q_PROPERTY(QString appVersion READ appVersion CONSTANT)
    Q_PROPERTY(QString machineIdSuffix READ machineIdSuffix CONSTANT)
    // Owner step-up prompt state (no code is ever exposed through a property).
    Q_PROPERTY(bool stepUpPending READ stepUpPending NOTIFY stepUpChanged)
    Q_PROPERTY(QString stepUpPrompt READ stepUpPrompt NOTIFY stepUpChanged)
    Q_PROPERTY(QString stepUpError READ stepUpError NOTIFY stepUpChanged)
    // The last staff sign-in was refused for a missing/wrong owner code.
    Q_PROPERTY(bool staffLoginNeedsCode READ staffLoginNeedsCode NOTIFY stateChanged)

    // Screen data (contract §2-§6 response shapes, converted to QVariant for QML).
    Q_PROPERTY(QVariantMap metrics READ metrics NOTIFY dataChanged)
    Q_PROPERTY(QVariantMap license READ license NOTIFY dataChanged)
    Q_PROPERTY(QVariantList licenses READ licenses NOTIFY dataChanged)
    Q_PROPERTY(QVariantMap licenseResult READ licenseResult NOTIFY dataChanged)
    Q_PROPERTY(QVariantList createdKeys READ createdKeys NOTIFY dataChanged)
    Q_PROPERTY(QVariantList staffList READ staffList NOTIFY dataChanged)
    Q_PROPERTY(QVariantMap enrollSecret READ enrollSecret NOTIFY dataChanged)
    Q_PROPERTY(QVariantList auditRows READ auditRows NOTIFY dataChanged)
    Q_PROPERTY(QString auditCursor READ auditCursor NOTIFY dataChanged)
    Q_PROPERTY(QVariantMap config READ config NOTIFY dataChanged)
    Q_PROPERTY(QVariantMap totpEnrollment READ totpEnrollment NOTIFY dataChanged)
    Q_PROPERTY(QString rotatedSecret READ rotatedSecret NOTIFY dataChanged)
    Q_PROPERTY(QVariantMap caps READ caps NOTIFY dataChanged)
    Q_PROPERTY(QVariantMap usage READ usage NOTIFY dataChanged)
    // capability -> bool for the current role. A QML binding that calls can()
    // would never re-evaluate (a method call registers no dependency), so the
    // panels read this property instead and refresh on stateChanged.
    Q_PROPERTY(QVariantMap capabilities READ capabilities NOTIFY stateChanged)

public:
    enum class Mode {
        Owner,
        Staff,
    };
    Q_ENUM(Mode)

    static constexpr int kReasonMaxChars = 200;

    explicit AdminToolController(QString rootDir, Mode mode, QObject* parent = nullptr);

    [[nodiscard]] QString mode() const;
    [[nodiscard]] bool ownerMode() const noexcept { return mode_ == Mode::Owner; }
    [[nodiscard]] bool authenticated() const noexcept { return authenticated_; }
    [[nodiscard]] bool busy() const noexcept { return inflight_ > 0; }
    [[nodiscard]] QString role() const { return role_; }
    [[nodiscard]] QString staffName() const { return staffName_; }
    [[nodiscard]] QString staffId() const { return staffId_; }
    [[nodiscard]] bool ownerRoutes() const;
    [[nodiscard]] bool totpRequired() const noexcept { return totpRequired_; }
    [[nodiscard]] QString statusMessage() const { return statusMessage_; }
    [[nodiscard]] bool statusIsError() const noexcept { return statusIsError_; }
    [[nodiscard]] QString resultText() const { return resultText_; }
    [[nodiscard]] QString securityState() const { return securityState_; }
    [[nodiscard]] bool securityLockActive() const noexcept { return securityLockActive_; }
    [[nodiscard]] QString updateState() const { return updateState_; }
    [[nodiscard]] bool updateAvailable() const noexcept { return updateAvailable_; }
    [[nodiscard]] QString latestVersion() const { return latestVersion_; }
    [[nodiscard]] bool killSwitchEngaged() const;
    [[nodiscard]] QString killSwitchReason() const;
    [[nodiscard]] QString appVersion() const { return QStringLiteral(ORION_NATIVE_VERSION); }
    [[nodiscard]] QString machineIdSuffix() const;
    [[nodiscard]] bool stepUpPending() const noexcept { return stepUp_.armed; }
    [[nodiscard]] QString stepUpPrompt() const { return stepUp_.prompt; }
    [[nodiscard]] QString stepUpError() const { return stepUpError_; }
    [[nodiscard]] bool staffLoginNeedsCode() const noexcept { return staffLoginNeedsCode_; }

    [[nodiscard]] QVariantMap metrics() const { return metrics_; }
    [[nodiscard]] QVariantMap license() const { return license_; }
    [[nodiscard]] QVariantList licenses() const { return licenses_; }
    [[nodiscard]] QVariantMap licenseResult() const { return licenseResult_; }
    [[nodiscard]] QVariantList createdKeys() const { return createdKeys_; }
    [[nodiscard]] QVariantList staffList() const { return staffList_; }
    [[nodiscard]] QVariantMap enrollSecret() const { return enrollSecret_; }
    [[nodiscard]] QVariantList auditRows() const { return auditRows_; }
    [[nodiscard]] QString auditCursor() const { return auditCursor_; }
    [[nodiscard]] QVariantMap config() const { return config_; }
    [[nodiscard]] QVariantMap totpEnrollment() const { return totpEnrollment_; }
    [[nodiscard]] QString rotatedSecret() const { return rotatedSecret_; }
    [[nodiscard]] QVariantMap caps() const { return caps_; }
    [[nodiscard]] QVariantMap usage() const { return usage_; }
    [[nodiscard]] QVariantMap capabilities() const;

    // ---- capability display (contract §1 matrix; server-enforced) ----
    Q_INVOKABLE bool can(const QString& capability) const;
    Q_INVOKABLE bool reasonValid(const QString& reason) const;
    Q_INVOKABLE void copyToClipboard(const QString& text);
    Q_INVOKABLE QString formatTs(qlonglong unixSeconds) const;

    // ---- auth ----
    Q_INVOKABLE void ownerLogin(const QString& adminSecret, const QString& totpCode);
    Q_INVOKABLE void setTotpCode(const QString& totpCode);
    Q_INVOKABLE void staffEnroll(const QString& staffId, const QString& enrollKey);
    // ownerCode: only for an owner-role staff row when owner TOTP is required;
    // blank = not sent. Used for this one request and dropped.
    Q_INVOKABLE void staffLogin(const QString& staffId, const QString& ownerCode = QString());
    // Owner step-up: send the parked owner-sensitive action with this fresh code
    // (X-Orion-Admin-TOTP, this request only), or drop the parked action.
    Q_INVOKABLE void submitStepUp(const QString& code);
    Q_INVOKABLE void cancelStepUp();
    Q_INVOKABLE void refreshWhoami();
    Q_INVOKABLE void logout();

    // ---- dashboard (§6) ----
    Q_INVOKABLE void refreshMetrics();

    // ---- licenses (§2 license operations, §3 reset policy) ----
    // query: {key | email | discord_user_id | machine_id}
    Q_INVOKABLE void lookupLicense(const QVariantMap& query);
    Q_INVOKABLE void createLicenses(const QString& plan, int days, int count, const QString& discordUserId,
                                    const QString& email, const QString& note, const QString& reason);
    // action: revoke|unrevoke|extend|set_plan|freeze|unfreeze|transfer|reset_machine|set_reset_policy
    // extra carries the action-specific fields (days, plan, discord_user_id, email, force, ...).
    Q_INVOKABLE void licenseAction(const QString& action, const QString& key, const QString& reason,
                                   const QVariantMap& extra);
    // kind: machine|discord
    Q_INVOKABLE void blacklist(const QString& kind, const QString& id, const QString& reason, bool add);
    Q_INVOKABLE void clearCreatedKeys();

    // ---- staff (§2 staff management, owner) ----
    Q_INVOKABLE void refreshStaff();
    Q_INVOKABLE void createStaff(const QString& discordUserId, const QString& role, const QVariantMap& caps,
                                 const QString& reason);
    // action: disable|enable|set_role|set_caps|reset_machine|reissue_enrollment
    Q_INVOKABLE void staffAction(const QString& action, const QString& staffId, const QString& reason,
                                 const QVariantMap& extra);
    Q_INVOKABLE void clearEnrollSecret();

    // ---- audit (§4) ----
    // filters: {since, until, actor, action, target}; cursor empty = first page.
    Q_INVOKABLE void fetchAudit(const QVariantMap& filters, const QString& cursor, int limit);
    Q_INVOKABLE void clearAudit();

    // ---- config (§5) ----
    Q_INVOKABLE void refreshConfig();
    Q_INVOKABLE void updateConfig(const QVariantMap& patch, const QString& reason);
    Q_INVOKABLE void setGlobalKill(bool engaged, const QString& reason);
    Q_INVOKABLE void totpEnroll(const QString& reason);
    Q_INVOKABLE void totpConfirm(const QString& code, const QString& reason);
    Q_INVOKABLE void rotateAdminSecret(const QString& reason);
    Q_INVOKABLE void clearTotpEnrollment();
    Q_INVOKABLE void clearRotatedSecret();

    // ---- tool housekeeping ----
    Q_INVOKABLE void checkUpdate();
    Q_INVOKABLE void startUpdate();
    Q_INVOKABLE void refreshSecurity();
    Q_INVOKABLE void clearResult();

signals:
    void stateChanged();
    void dataChanged();
    void stepUpChanged();
    // tag = the request tag (see the .cpp), ok = server said ok:true.
    void requestFinished(const QString& tag, bool ok, const QVariantMap& data);

private:
    enum class AuthKind {
        None,
        Owner,   // owner routes: admin secret (+TOTP) or an owner-role staff token
        Staff,   // staff routes: bearer + X-Machine-Id
    };

    struct PendingRequest {
        QString tag;
        QString path;
        QJsonObject body;
        QJsonObject context;
    };

    void beginRequest();
    void endRequest();
    void setStatus(QString value, bool isError = false);
    void setResult(QString value);
    bool requireUsableSecurity();
    bool requireAuth(bool ownerRoutesNeeded);
    bool requireReason(const QString& reason);
    // stepUpCode: a fresh owner code for THIS request only (X-Orion-Admin-TOTP on
    // an Owner request). Never stored.
    void applyAuth(QNetworkRequest& request, AuthKind authKind, const QString& stepUpCode = QString()) const;
    void sendRequest(const QString& tag, const QByteArray& method, const QString& path, const QJsonObject& query,
                     const QJsonObject& body, AuthKind authKind, const QJsonObject& context = {},
                     const QString& stepUpCode = QString());
    // POST an owner route; when `sensitive` and a code is needed, park it and ask QML.
    void sendOwnerAction(const QString& tag, const QString& path, const QJsonObject& body,
                         const QJsonObject& context, bool sensitive, const QString& prompt);
    [[nodiscard]] bool stepUpPromptNeeded() const;
    [[nodiscard]] QString staffRoleFor(const QString& staffId) const;
    void armStepUp(const QString& tag, const QString& path, const QJsonObject& body, const QJsonObject& context,
                   const QString& prompt, const QString& error);
    void clearStepUp();
    void handleResponse(const PendingRequest& pending, const QByteArray& payload, int httpStatus, bool ok);
    void handleSuccess(const PendingRequest& pending, const QJsonObject& obj);
    void licenseRequest(QJsonObject body, const QString& tag, const QJsonObject& context = {});
    QString licensePath() const;
    AuthKind licenseAuth() const;
    QJsonObject freshnessFields() const;
    QString apiBase() const;
    QString machineId() const;
    bool devBuild() const;
    void startUpdaterForSelf();
    void reportTamperIfAuthenticated(const SecurityStatus& status);
    void resetSessionData();

    QString rootDir_;
    Mode mode_ = Mode::Staff;
    SecurityManager security_;
    LicenseClient licenseClient_;
    QNetworkAccessManager network_;

    QString ownerSecret_;
    QString ownerTotp_;
    QString staffToken_;
    QString staffId_;
    QString staffDiscordId_;
    QString role_;
    QString staffName_;
    bool totpRequired_ = false;
    bool staffLoginNeedsCode_ = false;

    // The parked owner-sensitive action. Holds the request, NEVER the code.
    struct StepUpCall {
        bool armed = false;
        QString tag;
        QString path;
        QJsonObject body;
        QJsonObject context;
        QString prompt;
    };
    StepUpCall stepUp_;
    QString stepUpError_;

    QString statusMessage_;
    bool statusIsError_ = false;
    QString resultText_;
    QString securityState_;
    bool securityLockActive_ = false;
    bool authenticated_ = false;
    int inflight_ = 0;
    QString updateState_ = QStringLiteral("Unchecked");
    bool updateAvailable_ = false;
    QString latestVersion_;
    QString lastTamperReportFingerprint_;
    bool tamperReportInFlight_ = false;

    QVariantMap metrics_;
    QVariantMap license_;
    QVariantList licenses_;
    QVariantMap licenseResult_;
    QVariantList createdKeys_;
    QVariantList staffList_;
    QVariantMap enrollSecret_;
    QVariantList auditRows_;
    QString auditCursor_;
    QVariantMap config_;
    QVariantMap totpEnrollment_;
    QString rotatedSecret_;
    QVariantMap caps_;
    QVariantMap usage_;
};

} // namespace orion
