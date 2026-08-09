#pragma once

#include "LicenseClient.h"
#include "SecurityManager.h"

#include <QtCore/QJsonObject>
#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtNetwork/QNetworkAccessManager>

namespace orion {

class AdminToolController final : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString mode READ mode CONSTANT)
    Q_PROPERTY(bool ownerMode READ ownerMode CONSTANT)
    Q_PROPERTY(bool authenticated READ authenticated NOTIFY stateChanged)
    Q_PROPERTY(bool busy READ busy NOTIFY stateChanged)
    Q_PROPERTY(QString role READ role NOTIFY stateChanged)
    Q_PROPERTY(QString staffName READ staffName NOTIFY stateChanged)
    Q_PROPERTY(QString statusMessage READ statusMessage NOTIFY stateChanged)
    Q_PROPERTY(QString resultText READ resultText NOTIFY stateChanged)
    Q_PROPERTY(QString securityState READ securityState NOTIFY stateChanged)
    Q_PROPERTY(bool securityLockActive READ securityLockActive NOTIFY stateChanged)
    Q_PROPERTY(QString updateState READ updateState NOTIFY stateChanged)
    Q_PROPERTY(bool updateAvailable READ updateAvailable NOTIFY stateChanged)
    Q_PROPERTY(QString latestVersion READ latestVersion NOTIFY stateChanged)
    Q_PROPERTY(bool killSwitchEngaged READ killSwitchEngaged NOTIFY stateChanged)
    Q_PROPERTY(QString killSwitchReason READ killSwitchReason NOTIFY stateChanged)
    Q_PROPERTY(QString appVersion READ appVersion CONSTANT)
    Q_PROPERTY(QString machineIdSuffix READ machineIdSuffix CONSTANT)

public:
    enum class Mode {
        Owner,
        Staff,
    };
    Q_ENUM(Mode)

    explicit AdminToolController(QString rootDir, Mode mode, QObject* parent = nullptr);

    [[nodiscard]] QString mode() const;
    [[nodiscard]] bool ownerMode() const noexcept { return mode_ == Mode::Owner; }
    [[nodiscard]] bool authenticated() const noexcept { return authenticated_; }
    [[nodiscard]] bool busy() const noexcept { return busy_; }
    [[nodiscard]] QString role() const { return role_; }
    [[nodiscard]] QString staffName() const { return staffName_; }
    [[nodiscard]] QString statusMessage() const { return statusMessage_; }
    [[nodiscard]] QString resultText() const { return resultText_; }
    [[nodiscard]] QString securityState() const { return securityState_; }
    [[nodiscard]] bool securityLockActive() const noexcept { return securityLockActive_; }
    [[nodiscard]] QString updateState() const { return updateState_; }
    [[nodiscard]] bool updateAvailable() const noexcept { return updateAvailable_; }
    [[nodiscard]] QString latestVersion() const { return latestVersion_; }
    [[nodiscard]] bool killSwitchEngaged() const noexcept { return killSwitchEngaged_; }
    [[nodiscard]] QString killSwitchReason() const { return killSwitchReason_; }
    [[nodiscard]] QString appVersion() const { return QStringLiteral(ORION_NATIVE_VERSION); }
    [[nodiscard]] QString machineIdSuffix() const;

    Q_INVOKABLE void ownerLogin(const QString& adminSecret);
    Q_INVOKABLE void staffEnroll(const QString& discordUserId, const QString& enrollmentKey);
    Q_INVOKABLE void staffLogin(const QString& discordUserId);
    Q_INVOKABLE void logout();

    Q_INVOKABLE void createStaff(const QString& discordUserId, const QString& displayName, const QString& role);
    Q_INVOKABLE void disableStaff(const QString& staffOrDiscordId, const QString& reason);
    Q_INVOKABLE void resetStaffMachine(const QString& staffOrDiscordId, const QString& reason);
    Q_INVOKABLE void reissueStaffEnrollment(const QString& staffOrDiscordId, const QString& reason);

    Q_INVOKABLE void lookupLicense(const QString& licenseKey);
    Q_INVOKABLE void resetLicenseHwid(const QString& licenseKey, const QString& reason);
    Q_INVOKABLE void deactivateLicense(const QString& licenseKey, const QString& reason);

    Q_INVOKABLE void checkUpdate();
    Q_INVOKABLE void startUpdate();
    Q_INVOKABLE void refreshKillSwitch();
    Q_INVOKABLE void engageKillSwitch(const QString& reason);
    Q_INVOKABLE void releaseKillSwitch();
    Q_INVOKABLE void refreshSecurity();
    Q_INVOKABLE void clearResult();

signals:
    void stateChanged();

private:
    enum class AuthKind {
        None,
        OwnerSecret,
        StaffToken,
    };

    void setBusy(bool value);
    void setStatus(QString value);
    void setResult(QString value);
    bool requireUsableSecurity();
    void postJson(const QString& path, QJsonObject body, AuthKind authKind);
    void getJson(const QString& path, const QJsonObject& query, AuthKind authKind);
    void handleResponse(const QString& path, const QByteArray& payload, int httpStatus, bool ok);
    QJsonObject freshnessFields() const;
    QString apiBase() const;
    QString machineId() const;
    bool devBuild() const;
    void startUpdaterForSelf();
    void reportTamperIfAuthenticated(const SecurityStatus& status);

    QString rootDir_;
    Mode mode_ = Mode::Staff;
    SecurityManager security_;
    LicenseClient licenseClient_;
    QNetworkAccessManager network_;
    QString ownerSecret_;
    QString staffToken_;
    QString staffDiscordId_;
    QString role_;
    QString staffName_;
    QString statusMessage_;
    QString resultText_;
    QString securityState_;
    bool securityLockActive_ = false;
    bool authenticated_ = false;
    bool busy_ = false;
    QString updateState_ = QStringLiteral("Unchecked");
    bool updateAvailable_ = false;
    QString latestVersion_;
    bool killSwitchEngaged_ = false;
    QString killSwitchReason_;
    QString lastTamperReportFingerprint_;
    bool tamperReportInFlight_ = false;
};

} // namespace orion
