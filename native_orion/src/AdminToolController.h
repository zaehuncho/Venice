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

// Client controller for OrionOwner.exe / OrionStaff.exe. Implements the client
// side of docs/ADMIN_PANEL_V2_CONTRACT.md §7 against the endpoints in §2-§6.
//
// Auth model:
//   * Owner (break-glass): X-Orion-Admin-Secret (+ X-Orion-Admin-TOTP when the
//     owner has enrolled TOTP). Both are prompted and held in memory only.
//   * Staff: Bearer token + X-Machine-Id (the machine the token was issued to).
//     A staff row with role=owner may use the owner routes with the same token.
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
    Q_INVOKABLE void staffLogin(const QString& staffId);
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
    void applyAuth(QNetworkRequest& request, AuthKind authKind) const;
    void sendRequest(const QString& tag, const QByteArray& method, const QString& path, const QJsonObject& query,
                     const QJsonObject& body, AuthKind authKind, const QJsonObject& context = {});
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
