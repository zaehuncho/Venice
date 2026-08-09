// [VENICE_PROFILE 2026-08-08] Contract tests for the venice-profile.json export/import
// (src/VeniceProfile.h). Three contracts, mirrored by tests/test_venice_profile.py
// (which pins the same strings in CI and runs this binary on the rig via ctest):
//
//   1. veniceProfileRoundTrips — export -> import onto a fresh config reproduces the
//      exported surface BIT-IDENTICALLY (the export is deterministic, so the JSON
//      payloads are compared wholesale), and importing a profile onto the very config
//      that produced it is a no-op.
//   2. veniceProfileImportClampsOutOfRangeValues — a hostile/corrupt profile can never
//      install a value the settings loader would refuse: everything clamps into the
//      AppConfig load bands and each clamp/reject is reported in the notes.
//   3. veniceProfileNeverExportsLicense — the payload is an allowlist. The exact key
//      set is snapshotted, and rig-identifying / secret-adjacent AppConfig fields
//      (console IP, chiaki path, remote-play profile) are proven absent by sentinel.
//
// Headless: pure AppConfigData/LearningData structs + QJson — no controller, no
// engine, no files.

#include "VeniceProfile.h"

#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QSet>
#include <QtTest/QTest>

using orion::AppConfigData;
using orion::LearningData;
using orion::veniceProfileExport;
using orion::veniceProfileImport;

namespace {

// A config with a distinctive, in-band value in every profile-covered field, so the
// round trip cannot pass by accident of defaults.
AppConfigData tunedConfig()
{
    AppConfigData data;
    data.actuationLeadMs = 540.5;
    data.actuationLeadUserSet = true;
    data.tipTimingUserSet = true;
    data.tipPhaseAimFrozen = true;
    data.meterDelayEnabled = false;
    data.meterDelayMs = 320;
    data.meterDelayBypassOnDefense = false;
    data.noDipLeadMs = 12.5;
    data.pressAnchoredPredictorEnabled = true;
    data.pressAnchoredTipMs.insert(QStringLiteral("Standstill"), 655.0);
    data.pressAnchoredTipMs.insert(QStringLiteral("Right Fade"), 892.25);
    data.pressAnchoredTipSigmaMs.insert(QStringLiteral("Standstill"), 18.5);
    data.pressAnchoredTipN.insert(QStringLiteral("Standstill"), 42.0);
    data.shotTypeOffsets.insert(QStringLiteral("Standstill"), -7.5);
    data.shotTypeOffsets.insert(QStringLiteral("Left Fade"), 11.0);
    return data;
}

LearningData tunedLearning()
{
    LearningData learning;
    learning.learnedPhasePhysicalMs = 303.2; // the owner's rig canonical value 2026-08-06
    return learning;
}

QJsonObject minimalProfile()
{
    QJsonObject profile;
    profile.insert(QStringLiteral("kind"), QLatin1String(orion::kVeniceProfileKind));
    profile.insert(QStringLiteral("profile_version"), orion::kVeniceProfileVersion);
    return profile;
}

} // namespace

class VeniceProfileTests : public QObject {
    Q_OBJECT

private slots:
    void veniceProfileRoundTrips()
    {
        const AppConfigData source = tunedConfig();
        const LearningData sourceLearning = tunedLearning();
        const QJsonObject exported = veniceProfileExport(source, sourceLearning);

        // Import onto a FRESH install (compiled defaults).
        AppConfigData restored;
        LearningData restoredLearning;
        const auto result = veniceProfileImport(exported, restored, restoredLearning);
        QVERIFY2(result.ok, qPrintable(result.error));
        QVERIFY2(result.notes.isEmpty(),
                 qPrintable(QStringLiteral("in-band profile produced notes: %1")
                                .arg(result.notes.join(QStringLiteral(" | ")))));

        // Bit-identical: the deterministic export of the restored config IS the file.
        QCOMPARE(veniceProfileExport(restored, restoredLearning), exported);

        // Spot-check the values actually landed (not just a symmetric omission).
        QCOMPARE(restored.actuationLeadMs, 540.5);
        QVERIFY(restored.actuationLeadUserSet);
        QCOMPARE(restoredLearning.learnedPhasePhysicalMs, 303.2);
        QVERIFY(restored.tipPhaseAimFrozen);
        QCOMPARE(restored.meterDelayMs, 320);
        QVERIFY(!restored.meterDelayEnabled);
        QVERIFY(!restored.meterDelayBypassOnDefense);
        QCOMPARE(restored.pressAnchoredTipMs.value(QStringLiteral("Right Fade")), 892.25);
        QCOMPARE(restored.pressAnchoredTipN.value(QStringLiteral("Standstill")), 42.0);
        QCOMPARE(restored.shotTypeOffsets.value(QStringLiteral("Standstill")), -7.5);

        // Importing a profile onto the config that produced it is a no-op.
        AppConfigData same = source;
        LearningData sameLearning = sourceLearning;
        const auto again = veniceProfileImport(exported, same, sameLearning);
        QVERIFY(again.ok);
        QCOMPARE(veniceProfileExport(same, sameLearning), exported);

        // A never-measured tip round-trips as "absent", not as a bogus number.
        LearningData unmeasured; // learnedPhasePhysicalMs = -1
        const QJsonObject exportedUnmeasured = veniceProfileExport(source, unmeasured);
        QVERIFY(!exportedUnmeasured.contains(QStringLiteral("tip_timing_ms")));
        AppConfigData b2;
        LearningData lb2;
        QVERIFY(veniceProfileImport(exportedUnmeasured, b2, lb2).ok);
        QCOMPARE(lb2.learnedPhasePhysicalMs, -1.0);
    }

    void veniceProfileImportClampsOutOfRangeValues()
    {
        QJsonObject profile = minimalProfile();
        profile.insert(QStringLiteral("actuation_lead_ms"), 9999.0);
        profile.insert(QStringLiteral("meter_delay_ms"), 5);
        profile.insert(QStringLiteral("tip_timing_ms"), 9999.0);
        profile.insert(QStringLiteral("no_dip_lead_ms"), -999.0);
        profile.insert(QStringLiteral("meter_delay_enabled"),
                       QStringLiteral("yes")); // wrong type -> rejected, not coerced
        {
            QJsonObject press;
            press.insert(QStringLiteral("Standstill"), 99999.0);
            profile.insert(QStringLiteral("press_anchored_tip_ms"), press);
        }

        AppConfigData data;
        const bool meterDelayEnabledBefore = data.meterDelayEnabled;
        LearningData learning;
        const auto result = veniceProfileImport(profile, data, learning);
        QVERIFY2(result.ok, qPrintable(result.error));

        // The headline contract: lead 9999 -> the 800 ceiling, never installed raw.
        QCOMPARE(data.actuationLeadMs, AppConfigData::kActuationLeadMaxMs);
        QCOMPARE(data.actuationLeadMs, 800.0);
        QCOMPARE(data.meterDelayMs, AppConfigData::kMeterDelayMinMs); // 5 -> 100
        QCOMPARE(learning.learnedPhasePhysicalMs, orion::kVeniceProfileTipTimingMaxMs);
        QCOMPARE(data.noDipLeadMs, -orion::kVeniceProfileNoDipLeadBandMs);
        QCOMPARE(data.pressAnchoredTipMs.value(QStringLiteral("Standstill")),
                 orion::kVeniceProfilePressTipMaxMs);
        QCOMPARE(data.meterDelayEnabled, meterDelayEnabledBefore); // type-rejected

        // Every clamp/reject is REPORTED (the caller logs these).
        const QString joined = result.notes.join(QLatin1Char('\n'));
        QVERIFY2(joined.contains(QLatin1String("actuation_lead_ms")), qPrintable(joined));
        QVERIFY2(joined.contains(QLatin1String("meter_delay_ms")), qPrintable(joined));
        QVERIFY2(joined.contains(QLatin1String("tip_timing_ms")), qPrintable(joined));
        QVERIFY2(joined.contains(QLatin1String("meter_delay_enabled")), qPrintable(joined));

        // A lead in the (0, 150) dead band clamps UP (same policy as the load path).
        QJsonObject low = minimalProfile();
        low.insert(QStringLiteral("actuation_lead_ms"), 15.0);
        AppConfigData lowData;
        LearningData lowLearning;
        QVERIFY(veniceProfileImport(low, lowData, lowLearning).ok);
        QCOMPARE(lowData.actuationLeadMs, AppConfigData::kActuationLeadMinMs);

        // A zero lead means "never configured" and must not wipe a configured target.
        QJsonObject zero = minimalProfile();
        zero.insert(QStringLiteral("actuation_lead_ms"), 0.0);
        AppConfigData configured;
        configured.actuationLeadMs = 540.0;
        configured.actuationLeadUserSet = true;
        LearningData zl;
        QVERIFY(veniceProfileImport(zero, configured, zl).ok);
        QCOMPARE(configured.actuationLeadMs, 540.0);
        QVERIFY(configured.actuationLeadUserSet);

        // Not a profile at all -> hard refusal, zero mutation.
        QJsonObject junk;
        junk.insert(QStringLiteral("actuation_lead_ms"), 300.0);
        AppConfigData untouched;
        LearningData untouchedLearning;
        const auto refused = veniceProfileImport(junk, untouched, untouchedLearning);
        QVERIFY(!refused.ok);
        QVERIFY(!refused.error.isEmpty());
        QCOMPARE(untouched.actuationLeadMs, AppConfigData{}.actuationLeadMs);
    }

    void veniceProfileNeverExportsLicense()
    {
        // Plant secret-adjacent sentinels in every rig/account-identifying AppConfig
        // field. None of them may reach the payload — the export is an allowlist, so
        // this holds even though these fields sit in the same struct.
        AppConfigData data = tunedConfig();
        data.chiakiPath = QStringLiteral("SENTINEL-CHIAKI-PATH");
        data.remotePlayConsoleIp = QStringLiteral("SENTINEL-CONSOLE-IP");
        data.remotePlayProfile = QStringLiteral("SENTINEL-PSN-PROFILE");
        const QJsonObject exported = veniceProfileExport(data, tunedLearning());
        const QString text = QString::fromUtf8(
            QJsonDocument(exported).toJson(QJsonDocument::Indented));

        QVERIFY(!text.contains(QLatin1String("SENTINEL-"), Qt::CaseInsensitive));
        // License/entitlement/token state lives in SecurityManager/LicenseClient, not
        // AppConfig — the export cannot even reach it. Pin the vocabulary anyway so a
        // future field addition that smuggles one of these words fails loudly.
        QVERIFY(!text.contains(QLatin1String("license"), Qt::CaseInsensitive));
        QVERIFY(!text.contains(QLatin1String("entitlement"), Qt::CaseInsensitive));
        QVERIFY(!text.contains(QLatin1String("bearer"), Qt::CaseInsensitive));
        QVERIFY(!text.contains(QLatin1String("nexus_bridge"), Qt::CaseInsensitive));

        // Exact allowlist snapshot: any NEW export key must be added here, which is
        // the review moment where "is this customer tuning or a secret?" gets asked.
        QSet<QString> keys;
        for (auto it = exported.constBegin(); it != exported.constEnd(); ++it) {
            keys.insert(it.key());
        }
        const QSet<QString> allowlist = {
            QStringLiteral("_comment"),
            QStringLiteral("kind"),
            QStringLiteral("profile_version"),
            QStringLiteral("actuation_lead_ms"),
            QStringLiteral("actuation_lead_user_set"),
            QStringLiteral("tip_timing_ms"),
            QStringLiteral("tip_timing_user_set"),
            QStringLiteral("tip_phase_aim_frozen"),
            QStringLiteral("meter_delay_enabled"),
            QStringLiteral("meter_delay_ms"),
            QStringLiteral("meter_delay_bypass_on_defense"),
            QStringLiteral("no_dip_lead_ms"),
            QStringLiteral("press_anchored_predictor_enabled"),
            QStringLiteral("press_anchored_tip_ms"),
            QStringLiteral("press_anchored_tip_sigma_ms"),
            QStringLiteral("press_anchored_tip_n"),
            QStringLiteral("shot_type_offsets"),
        };
        QCOMPARE(keys, allowlist);
    }
};

QTEST_GUILESS_MAIN(VeniceProfileTests)
#include "VeniceProfileTests.moc"
