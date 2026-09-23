// [rc1 RT-CRIT-01 client, P-F] Owner step-up policy for OrionOwner/OrionStaff.
// Exercises the pure orion::admin_step_up functions in AdminToolController.h:
// which owner actions need a fresh owner TOTP code, when the console asks for
// one, code sanitising, and the plain refusal messages. No network, no QML.
#include "AdminToolController.h"

#include <QtTest/QtTest>

using namespace orion::admin_step_up;

class AdminStepUpPolicyTests final : public QObject {
    Q_OBJECT

private slots:
    void configActionsNeedStepUp()
    {
        QVERIFY(configActionNeedsStepUp(QStringLiteral("totp_enroll")));
        QVERIFY(configActionNeedsStepUp(QStringLiteral("totp_confirm")));
        QVERIFY(configActionNeedsStepUp(QStringLiteral("totp_disable")));
        QVERIFY(configActionNeedsStepUp(QStringLiteral("rotate_admin_secret")));
        QVERIFY(!configActionNeedsStepUp(QStringLiteral("set")));
        QVERIFY(!configActionNeedsStepUp(QString()));
    }

    void configPatchSensitiveKeys()
    {
        QVERIFY(configPatchNeedsStepUp({ { QStringLiteral("owner_totp_required"), false } }));
        QVERIFY(configPatchNeedsStepUp({ { QStringLiteral("owner_ip_allowlist"), QVariantList{} } }));
        QVERIFY(configPatchNeedsStepUp({ { QStringLiteral("alerts"), QVariantMap{} } }));
        QVERIFY(!configPatchNeedsStepUp({ { QStringLiteral("motd"), QVariantMap{} } }));
        QVERIFY(!configPatchNeedsStepUp({ { QStringLiteral("min_client_version"), QStringLiteral("1.0.0") } }));
    }

    void releasingTheKillNeedsStepUpEngagingDoesNot()
    {
        QVERIFY(configPatchNeedsStepUp({ { QStringLiteral("global_kill"), false } }));
        QVERIFY(!configPatchNeedsStepUp({ { QStringLiteral("global_kill"), true } }));
        QVariantMap off;
        off.insert(QStringLiteral("enabled"), false);
        QVariantMap on;
        on.insert(QStringLiteral("enabled"), true);
        QVERIFY(configPatchNeedsStepUp({ { QStringLiteral("global_kill"), off } }));
        QVERIFY(!configPatchNeedsStepUp({ { QStringLiteral("global_kill"), on } }));
    }

    void staffRules()
    {
        QVERIFY(staffCreateNeedsStepUp(QStringLiteral("owner")));
        QVERIFY(!staffCreateNeedsStepUp(QStringLiteral("admin")));
        QVERIFY(!staffCreateNeedsStepUp(QStringLiteral("support")));

        QVERIFY(staffActionNeedsStepUp(QStringLiteral("disable"), QStringLiteral("owner"), {}));
        QVERIFY(staffActionNeedsStepUp(QStringLiteral("reissue_enrollment"), QStringLiteral("owner"), {}));
        QVERIFY(!staffActionNeedsStepUp(QStringLiteral("disable"), QStringLiteral("support"), {}));
        QVERIFY(staffActionNeedsStepUp(QStringLiteral("disable"), QString(), {})); // unknown row
        QVERIFY(staffActionNeedsStepUp(QStringLiteral("set_role"), QStringLiteral("support"),
                                       { { QStringLiteral("role"), QStringLiteral("owner") } }));
        QVERIFY(!staffActionNeedsStepUp(QStringLiteral("set_role"), QStringLiteral("support"),
                                        { { QStringLiteral("role"), QStringLiteral("admin") } }));
    }

    void promptPolicy()
    {
        // Break-glass: the admin secret is the possession factor; ask only when TOTP is required.
        QVERIFY(!shouldPromptForCode(true, false, -1));
        QVERIFY(!shouldPromptForCode(true, false, 0));
        QVERIFY(shouldPromptForCode(true, true, -1));
        QVERIFY(shouldPromptForCode(true, false, 1));
        // Owner-role bearer: always ask unless config positively says TOTP is off.
        QVERIFY(shouldPromptForCode(false, false, -1));
        QVERIFY(shouldPromptForCode(false, false, 1));
        QVERIFY(!shouldPromptForCode(false, false, 0));
        QVERIFY(shouldPromptForCode(false, true, 0));
    }

    void codeSanitising()
    {
        QCOMPARE(sanitizeCode(QStringLiteral(" 123 456 ")), QStringLiteral("123456"));
        QVERIFY(codeWellFormed(sanitizeCode(QStringLiteral("123-456"))));
        QVERIFY(!codeWellFormed(sanitizeCode(QStringLiteral("12345"))));
        QVERIFY(!codeWellFormed(sanitizeCode(QStringLiteral("1234567"))));
        QVERIFY(!codeWellFormed(QString()));
    }

    void plainMessages()
    {
        const QString plain = QStringLiteral("That code was already used or is wrong — wait for the next code.");
        QCOMPARE(plainMessage(QStringLiteral("totp_replayed")), plain);
        QCOMPARE(plainMessage(QStringLiteral("invalid_totp")), plain);
        QVERIFY(!plainMessage(QStringLiteral("step_up_required")).isEmpty());
        QVERIFY(!plainMessage(QStringLiteral("config_unavailable")).isEmpty());
        QVERIFY(!plainMessage(QStringLiteral("audit_unavailable")).isEmpty());
        QVERIFY(plainMessage(QStringLiteral("forbidden")).isEmpty());
        QVERIFY(codeRetryable(QStringLiteral("totp_replayed")));
        QVERIFY(codeRetryable(QStringLiteral("invalid_totp")));
        QVERIFY(codeRetryable(QStringLiteral("totp_required")));
        QVERIFY(!codeRetryable(QStringLiteral("step_up_required")));
        QVERIFY(!codeRetryable(QStringLiteral("step_up_unavailable")));
    }
};

QTEST_GUILESS_MAIN(AdminStepUpPolicyTests)
#include "AdminStepUpPolicyTests.moc"
