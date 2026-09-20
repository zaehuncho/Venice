#include "AppConfig.h"
#include "PacketBridgeAuthority.h"
#include "RemotePlayExecutablePolicy.h"
#include "SidecarReaderProfile.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QTemporaryDir>
#include <QtTest/QTest>

using namespace orion;

namespace {

bool createExecutableFixture(const QString& path, const QByteArray& contents)
{
    const QFileInfo info(path);
    if (!QDir().mkpath(info.absolutePath())) {
        return false;
    }
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return false;
    }
    return file.write(contents) == contents.size();
}

QString canonical(const QString& path)
{
    return QDir::toNativeSeparators(QFileInfo(path).canonicalFilePath());
}

// [ORION_PILL_YOLO_ROUTE 2026-09-17] Put the proposer setting into a child environment
// exactly the way RemotePlaySession::startSidecar does, so the style route below is always
// tested against the environment the launch really hands it. Wrapped because
// applyMeterProposerSetting() is [[nodiscard]]: its return value IS the launch log line.
void seedProposerEnv(QProcessEnvironment& env, const QString& proposerSetting)
{
    const QString resolved = applyMeterProposerSetting(env, proposerSetting, true);
    QVERIFY(resolved.startsWith(QStringLiteral("ORION_METER_PROPOSER=")));
}

} // namespace

class RemotePlayExecutablePolicyTests final : public QObject {
    Q_OBJECT

private slots:
    void networkShipsOnAndLegacyCaptureOptOutIsRetired()
    {
#ifdef ORION_PRODUCTION_BUILD
        // Drives AppConfig through a temporary root. In a production build
        // AppConfig::settingsPath() resolves via orionDataDir(), which returns the per-user
        // %LOCALAPPDATA% dir and IGNORES rootDir by design (OrionPaths.h): a production
        // install lives in Program Files, which a normal user cannot write. The fixture
        // settings.json is therefore never read and load() returns false. The contract is
        // still fully proven by the dev build, which runs this test unchanged.
        QSKIP("AppConfig settings path redirects to the per-user data dir in production");
#endif
        // [VENICENET WAVE 1 2026-08-08, owner-directed] The passive-sniffing opt-in flag
        // (passiveSniffingEnabled / "network_packet_capture_opt_in") is DELETED, not
        // defaulted: the passive-sniffing legacy gate is retired and everything
        // network-side ships ON. This test pins both halves of that decision so neither
        // can drift back silently:
        //   1. the network feature defaults ON, and
        //   2. a legacy settings.json still carrying the old opt-out key no longer turns
        //      anything off — the key is ignored on load, by design.
        const AppConfigData defaults;
        QVERIFY2(defaults.networkEnabled,
                 "network features ship enabled by product decision; flipping this default "
                 "silently re-breaks court detection and Meter Delay for every user");

        // network_enabled itself still round-trips: an explicit false must load as false
        // (the network feature keeps a working opt-out even though the capture sub-flag
        // is gone).
        QTemporaryDir networkOffDir;
        QVERIFY(networkOffDir.isValid());
        const QJsonObject networkOff{
            {QStringLiteral("network_enabled"), false},
        };
        QVERIFY(createExecutableFixture(
            QDir(networkOffDir.path()).filePath(QStringLiteral("settings.json")),
            QJsonDocument(networkOff).toJson(QJsonDocument::Compact)));
        AppConfig disabled(networkOffDir.path());
        QVERIFY(disabled.load());
        QVERIFY2(!disabled.data().networkEnabled,
                 "an explicit network_enabled=false must still load as false");

        // The retired opt-out key must be inert: it neither disables the network side
        // nor survives a save (AppConfig::toJson no longer writes it).
        QTemporaryDir legacyDir;
        QVERIFY(legacyDir.isValid());
        const QJsonObject legacyOptOut{
            {QStringLiteral("network_packet_capture_opt_in"), false},
        };
        QVERIFY(createExecutableFixture(
            QDir(legacyDir.path()).filePath(QStringLiteral("settings.json")),
            QJsonDocument(legacyOptOut).toJson(QJsonDocument::Compact)));
        AppConfig legacy(legacyDir.path());
        QVERIFY(legacy.load());
        QVERIFY2(legacy.data().networkEnabled,
                 "the retired network_packet_capture_opt_in key must be ignored on load; "
                 "a legacy opt-out must not resurrect the deleted passive-sniffing gate");
    }

    void unverifiedBridgePacketCannotMutateTimingByDefault()
    {
        int timingMutationCount = 0;
        const bool forwarded = packet_bridge_authority::forwardToTimingIfAuthorized(
            packet_bridge_authority::ServiceIdentityTrust::Unverified,
            false,
            [&timingMutationCount]() { ++timingMutationCount; });

        QVERIFY(!forwarded);
        QCOMPARE(timingMutationCount, 0);
    }

    void productionCompilesOutUnverifiedBridgeTimingExperiment()
    {
        int timingMutationCount = 0;
        const bool forwarded = packet_bridge_authority::forwardToTimingIfAuthorized(
            packet_bridge_authority::ServiceIdentityTrust::Unverified,
            true,
            [&timingMutationCount]() { ++timingMutationCount; });

#ifdef ORION_PRODUCTION_BUILD
        QVERIFY(!forwarded);
        QCOMPARE(timingMutationCount, 0);
#else
        QVERIFY(forwarded);
        QCOMPARE(timingMutationCount, 1);
#endif
    }

    void verifiedServiceIdentityMayFeedTiming()
    {
        int timingMutationCount = 0;
        const bool forwarded = packet_bridge_authority::forwardToTimingIfAuthorized(
            packet_bridge_authority::ServiceIdentityTrust::VerifiedService,
            false,
            [&timingMutationCount]() { ++timingMutationCount; });

        QVERIFY(forwarded);
        QCOMPARE(timingMutationCount, 1);
    }

    void bundledImageHasPriorityOverRepositoryAndConfiguredPath()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString appDir = QDir(temp.path()).filePath(QStringLiteral("app"));
        const QString rootDir = QDir(temp.path()).filePath(QStringLiteral("repo"));
        const QString external = QDir(temp.path()).filePath(QStringLiteral("old/OrionStream.exe"));
        const QString packaged =
            remote_play_executable_policy::packagedOrionStreamPath(appDir);
        const QString repository =
            remote_play_executable_policy::repositoryOrionStreamPath(rootDir);

        QVERIFY(createExecutableFixture(packaged, QByteArrayLiteral("packaged")));
        QVERIFY(createExecutableFixture(repository, QByteArrayLiteral("repository")));
        QVERIFY(createExecutableFixture(external, QByteArrayLiteral("external")));

        const auto development = remote_play_executable_policy::select(
            appDir, rootDir, external, false);
        QCOMPARE(static_cast<int>(development.source),
                 static_cast<int>(remote_play_executable_policy::Source::Packaged));
        QCOMPARE(development.path, canonical(packaged));

        const auto production = remote_play_executable_policy::select(
            appDir, rootDir, external, true);
        QCOMPARE(static_cast<int>(production.source),
                 static_cast<int>(remote_play_executable_policy::Source::Packaged));
        QCOMPARE(production.path, canonical(packaged));
    }

    void productionRejectsExistingExternalAndRepositoryPathsWithoutBundle()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString appDir = QDir(temp.path()).filePath(QStringLiteral("app"));
        const QString rootDir = QDir(temp.path()).filePath(QStringLiteral("repo"));
        const QString external = QDir(temp.path()).filePath(QStringLiteral("old/OrionStream.exe"));
        const QString repository =
            remote_play_executable_policy::repositoryOrionStreamPath(rootDir);

        QVERIFY(QDir().mkpath(appDir));
        QVERIFY(createExecutableFixture(repository, QByteArrayLiteral("repository")));
        QVERIFY(createExecutableFixture(external, QByteArrayLiteral("external")));

        const auto selection = remote_play_executable_policy::select(
            appDir, rootDir, external, true);
        QCOMPARE(static_cast<int>(selection.source),
                 static_cast<int>(remote_play_executable_policy::Source::None));
        QVERIFY(selection.path.isEmpty());
    }

    void developmentRetainsConfiguredFallbackWhenNoLocalImageExists()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString appDir = QDir(temp.path()).filePath(QStringLiteral("app"));
        const QString rootDir = QDir(temp.path()).filePath(QStringLiteral("repo"));
        const QString external = QDir(temp.path()).filePath(QStringLiteral("dev/OrionStream.exe"));
        QVERIFY(QDir().mkpath(appDir));
        QVERIFY(QDir().mkpath(rootDir));
        QVERIFY(createExecutableFixture(external, QByteArrayLiteral("developer")));

        const auto selection = remote_play_executable_policy::select(
            appDir, rootDir, external, false);
        QCOMPARE(static_cast<int>(selection.source),
                 static_cast<int>(remote_play_executable_policy::Source::DeveloperConfigured));
        QCOMPARE(selection.path, canonical(external));
    }

    void decoderProducerExpectationCarriesExactSizeAndHash()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString image = QDir(temp.path()).filePath(QStringLiteral("OrionStream.exe"));
        QVERIFY(createExecutableFixture(image, QByteArrayLiteral("exact-decoder-image")));

        const auto identity = remote_play_executable_policy::readExecutableIdentity(image);
        QVERIFY(identity.valid);
        QCOMPARE(identity.size, qint64(19));
        QCOMPARE(identity.sha256.size(), 64);

        QJsonObject sidecar;
        QVERIFY(remote_play_executable_policy::insertDecoderPipeProducerExpectation(
            sidecar, image));
        QCOMPARE(sidecar.value(QStringLiteral("chiaki_identity_size")).toString(),
                 QString::number(identity.size));
        QCOMPARE(sidecar.value(QStringLiteral("chiaki_identity_sha256")).toString(),
                 identity.sha256);
    }

    void decoderProducerExpectationFailsClosedWhenImageMissing()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        QJsonObject sidecar;
        QVERIFY(!remote_play_executable_policy::insertDecoderPipeProducerExpectation(
            sidecar, QDir(temp.path()).filePath(QStringLiteral("missing.exe"))));
        QVERIFY(sidecar.value(QStringLiteral("chiaki_identity_size")).toString().isEmpty());
        QVERIFY(sidecar.value(QStringLiteral("chiaki_identity_sha256")).toString().isEmpty());
    }

    void decoderProducerHashRejectsSameSizeContentReplacement()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString image = QDir(temp.path()).filePath(QStringLiteral("OrionStream.exe"));
        QVERIFY(createExecutableFixture(image, QByteArrayLiteral("AAAA")));
        const auto first = remote_play_executable_policy::readExecutableIdentity(image);
        QVERIFY(first.valid);
        QVERIFY(createExecutableFixture(image, QByteArrayLiteral("BBBB")));
        const auto second = remote_play_executable_policy::readExecutableIdentity(image);
        QVERIFY(second.valid);
        QCOMPARE(first.size, second.size);
        QVERIFY(first.sha256 != second.sha256);
    }

    // [METER DETECTION CARD 2026-09-10] The sidecar env carries ORION_METER_PROPOSER from
    // the user's setting under the SidecarReaderProfile contract: the setting is the
    // source of truth, an explicit process value wins only in development, production
    // pins the setting, and nothing but "cv" | "yolo" ever reaches the child.
    void sidecarEnvCarriesMeterProposerFromSetting()
    {
        const QString key = QString::fromLatin1(kMeterProposerEnvKey);
        QCOMPARE(key, QStringLiteral("ORION_METER_PROPOSER"));

        // Silent environment: the setting is exported verbatim (normalised).
        {
            QProcessEnvironment env;
            const QString resolved = applyMeterProposerSetting(env, QStringLiteral("yolo"), true);
            QCOMPARE(env.value(key), QStringLiteral("yolo"));
            QCOMPARE(resolved, QStringLiteral("ORION_METER_PROPOSER=yolo(setting)"));
        }
        // Default setting -> the certified pure-CV proposer.
        {
            QProcessEnvironment env;
            const QString resolved = applyMeterProposerSetting(env, QStringLiteral("cv"), true);
            QCOMPARE(env.value(key), QStringLiteral("cv"));
            QVERIFY(resolved.endsWith(QStringLiteral("(setting)")));
        }
        // Development keeps an explicit launcher value (run_orion.local.ps1 A/B pin)...
        {
            QProcessEnvironment env;
            env.insert(key, QStringLiteral("cv"));
            const QString resolved = applyMeterProposerSetting(env, QStringLiteral("yolo"), true);
            QCOMPARE(env.value(key), QStringLiteral("cv"));
            QCOMPARE(resolved, QStringLiteral("ORION_METER_PROPOSER=cv(env)"));
        }
        // ...but never an unknown one: a garbage override is normalised, not forwarded.
        {
            QProcessEnvironment env;
            env.insert(key, QStringLiteral("banana"));
            const QString resolved = applyMeterProposerSetting(env, QStringLiteral("yolo"), true);
            QCOMPARE(env.value(key), QStringLiteral("cv"));
            QCOMPARE(resolved, QStringLiteral("ORION_METER_PROPOSER=cv(env)"));
        }
        // An EMPTY explicit value is silence, not an override.
        {
            QProcessEnvironment env;
            env.insert(key, QString{});
            const QString resolved = applyMeterProposerSetting(env, QStringLiteral("yolo"), true);
            QCOMPARE(env.value(key), QStringLiteral("yolo"));
            QVERIFY(resolved.endsWith(QStringLiteral("(setting)")));
        }
        // Production ignores the inherited process value entirely.
        {
            QProcessEnvironment env;
            env.insert(key, QStringLiteral("cv"));
            const QString resolved = applyMeterProposerSetting(env, QStringLiteral("yolo"), false);
            QCOMPARE(env.value(key), QStringLiteral("yolo"));
            QCOMPARE(resolved, QStringLiteral("ORION_METER_PROPOSER=yolo(production)"));
        }

#ifndef ORION_PRODUCTION_BUILD
        // End to end from settings.json: the loader normalises the raw key and the env
        // helper exports exactly that value.
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString rootDir = QDir(temp.path()).filePath(QStringLiteral("repo"));
        const QJsonObject settings{{QStringLiteral("meter_proposer"), QStringLiteral("YOLO")}};
        QVERIFY(createExecutableFixture(
            QDir(rootDir).filePath(QStringLiteral("settings.json")),
            QJsonDocument(settings).toJson(QJsonDocument::Compact)));
        AppConfig config(rootDir);
        QVERIFY(config.load());
        QCOMPARE(config.data().meterProposer, QStringLiteral("yolo"));
        QProcessEnvironment env;
        const QString resolved = applyMeterProposerSetting(env, config.data().meterProposer, true);
        QCOMPARE(env.value(key), QStringLiteral("yolo"));
        QCOMPARE(resolved, QStringLiteral("ORION_METER_PROPOSER=yolo(setting)"));
#endif
    }

    // [ORION_PILL_YOLO_ROUTE 2026-09-17] The Pill capsule is invisible to the CV contour
    // locator (0 proposals on 642 labelled Pill frames: a 3 px fill core against gate 4's
    // 8 px floor) and fully visible to the packaged ONNX detector (661/661, IoU 0.93).
    // So a Pill launch takes the proposer the measurement supports, whatever the persisted
    // meter_proposer says, and carries the style down beside it.
    void pillStyleRoutesTheSidecarOntoThePackagedDetector()
    {
        const QString proposerKey = QString::fromLatin1(kMeterProposerEnvKey);
        const QString styleKey = QString::fromLatin1(kMeterStyleEnvKey);
        QCOMPARE(styleKey, QStringLiteral("ORION_METER_STYLE"));
        QCOMPARE(QString::fromLatin1(kPillYoloRouteEnvKey),
                 QStringLiteral("ORION_PILL_YOLO_ROUTE"));
        const QString routeLog = QStringLiteral(
            "METER STYLE: Pill -> proposer=yolo (contour locator cannot see the Pill capsule; "
            "packaged detector is Pill-trained)");

        // The live hazard this closes: style Pill persisted beside the certified cv proposer.
        {
            QProcessEnvironment env;
            seedProposerEnv(env, QStringLiteral("cv"));
            QCOMPARE(env.value(proposerKey), QStringLiteral("cv"));
            const MeterStyleRouteResult route =
                applyPillYoloRoute(env, QStringLiteral("Pill"), true);
            QVERIFY(route.routed);
            QCOMPARE(env.value(proposerKey), QStringLiteral("yolo"));
            QCOMPARE(env.value(styleKey), QStringLiteral("pill"));
            QCOMPARE(route.resolvedProposer, QStringLiteral("yolo"));
            QCOMPARE(route.log, routeLog);
        }
        // The style arrives from a hand-edited settings.json in any casing/padding.
        {
            QProcessEnvironment env;
            seedProposerEnv(env, QStringLiteral("cv"));
            const MeterStyleRouteResult route =
                applyPillYoloRoute(env, QStringLiteral("  pILL "), true);
            QVERIFY(route.routed);
            QCOMPARE(env.value(proposerKey), QStringLiteral("yolo"));
            QCOMPARE(env.value(styleKey), QStringLiteral("pill"));
        }
        // An explicit development A/B pin on cv also loses to the style: an arm that
        // cannot see the meter measures nothing, so there is nothing to preserve.
        {
            QProcessEnvironment env;
            env.insert(proposerKey, QStringLiteral("cv"));
            seedProposerEnv(env, QStringLiteral("yolo"));
            QCOMPARE(env.value(proposerKey), QStringLiteral("cv"));
            const MeterStyleRouteResult route =
                applyPillYoloRoute(env, QStringLiteral("Pill"), true);
            QVERIFY(route.routed);
            QCOMPARE(env.value(proposerKey), QStringLiteral("yolo"));
        }
        // Already on yolo: the env is the same one either way, and the line still names
        // the route so a launch log always says which proposer the style chose.
        {
            QProcessEnvironment env;
            seedProposerEnv(env, QStringLiteral("yolo"));
            const MeterStyleRouteResult route =
                applyPillYoloRoute(env, QStringLiteral("Pill"), true);
            QVERIFY(route.routed);
            QCOMPARE(env.value(proposerKey), QStringLiteral("yolo"));
            QCOMPARE(env.value(styleKey), QStringLiteral("pill"));
            QCOMPARE(route.log, routeLog);
        }
    }

    // The other half of the contract, and the one that keeps the shipped style safe:
    // anything that is not Pill leaves the environment EXACTLY as the pre-route build
    // assembled it -- no forced proposer, no ORION_METER_STYLE, no log line.
    void nonPillStylesKeepTodaysSidecarEnvironmentByteForByte()
    {
        const QString styleKey = QString::fromLatin1(kMeterStyleEnvKey);
        const QStringList styles{QStringLiteral("Arrow2"), QStringLiteral("Straight"),
                                 QStringLiteral("Dial"),   QStringLiteral("Sword"),
                                 QString{}};
        const QStringList proposers{QStringLiteral("cv"), QStringLiteral("yolo")};
        for (const QString& style : styles) {
            for (const QString& proposer : proposers) {
                QProcessEnvironment env;
                seedProposerEnv(env, proposer);
                QStringList before = env.toStringList();
                before.sort();
                const MeterStyleRouteResult route = applyPillYoloRoute(env, style, true);
                QStringList after = env.toStringList();
                after.sort();
                QVERIFY2(after == before,
                         qPrintable(QStringLiteral("style %1 altered the sidecar env")
                                        .arg(style.isEmpty() ? QStringLiteral("<empty>") : style)));
                QVERIFY(!route.routed);
                QVERIFY(route.log.isEmpty());
                QVERIFY(!env.contains(styleKey));
                QCOMPARE(route.resolvedProposer, proposer);
            }
        }
    }

    // The kill switch. Down, it restores the pre-route environment byte for byte -- and
    // then the launch must SAY that the session is blind, which is the one thing the
    // shipped build never did.
    void pillYoloRouteKillSwitchRestoresThePreRouteEnvironmentAndSaysSo()
    {
        const QString proposerKey = QString::fromLatin1(kMeterProposerEnvKey);
        const QString styleKey = QString::fromLatin1(kMeterStyleEnvKey);
        const QString routeEnvKey = QString::fromLatin1(kPillYoloRouteEnvKey);

        // Setting down (pill_yolo_route=false): env untouched + the mismatch line.
        {
            QProcessEnvironment env;
            seedProposerEnv(env, QStringLiteral("cv"));
            QStringList before = env.toStringList();
            before.sort();
            const MeterStyleRouteResult route =
                applyPillYoloRoute(env, QStringLiteral("Pill"), false);
            QStringList after = env.toStringList();
            after.sort();
            QCOMPARE(after, before);
            QVERIFY(!route.routed);
            QVERIFY(!env.contains(styleKey));
            QCOMPARE(env.value(proposerKey), QStringLiteral("cv"));
            QCOMPARE(route.resolvedProposer, QStringLiteral("cv"));
            QVERIFY(route.log.startsWith(QStringLiteral("METER STYLE MISMATCH:")));
            QVERIFY(route.log.contains(QStringLiteral("style=Pill proposer=cv")));
        }
        // Env door (ORION_PILL_YOLO_ROUTE=0) with the setting still true.
        {
            QProcessEnvironment env;
            env.insert(routeEnvKey, QStringLiteral(" 0 "));
            seedProposerEnv(env, QStringLiteral("cv"));
            const MeterStyleRouteResult route =
                applyPillYoloRoute(env, QStringLiteral("Pill"), true);
            QVERIFY(!route.routed);
            QCOMPARE(env.value(proposerKey), QStringLiteral("cv"));
            QVERIFY(!env.contains(styleKey));
            QVERIFY(route.log.startsWith(QStringLiteral("METER STYLE MISMATCH:")));
        }
        // ...but ONLY an exact "0". A mistyped kill switch must not ship a blind proposer.
        for (const QString& noise : {QStringLiteral("false"), QStringLiteral("no"),
                                     QString{}, QStringLiteral("00")}) {
            QProcessEnvironment env;
            env.insert(routeEnvKey, noise);
            seedProposerEnv(env, QStringLiteral("cv"));
            const MeterStyleRouteResult route =
                applyPillYoloRoute(env, QStringLiteral("Pill"), true);
            QVERIFY2(route.routed, qPrintable(QStringLiteral("'%1' disabled the route").arg(noise)));
            QCOMPARE(env.value(proposerKey), QStringLiteral("yolo"));
        }
        // A killed route over a proposer that CAN see the Pill is not a mismatch: the
        // reader is not blind, so there is nothing to warn about.
        {
            QProcessEnvironment env;
            seedProposerEnv(env, QStringLiteral("yolo"));
            const MeterStyleRouteResult route =
                applyPillYoloRoute(env, QStringLiteral("Pill"), false);
            QVERIFY(!route.routed);
            QVERIFY(route.log.isEmpty());
            QCOMPARE(env.value(proposerKey), QStringLiteral("yolo"));
            QVERIFY(!env.contains(styleKey));
        }
    }

#ifndef ORION_PRODUCTION_BUILD
    // End to end from settings.json: the key round-trips, an absent key takes the
    // compiled default (true), and the loaded value is what the launch route reads.
    void pillYoloRouteSettingRoundTripsThroughSettingsJson()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());

        const QString killedRoot = QDir(temp.path()).filePath(QStringLiteral("killed"));
        const QJsonObject killed{{QStringLiteral("meter_style"), QStringLiteral("Pill")},
                                 {QStringLiteral("pill_yolo_route"), false}};
        QVERIFY(createExecutableFixture(
            QDir(killedRoot).filePath(QStringLiteral("settings.json")),
            QJsonDocument(killed).toJson(QJsonDocument::Compact)));
        AppConfig killedConfig(killedRoot);
        QVERIFY(killedConfig.load());
        QCOMPARE(killedConfig.data().meterStyle, QStringLiteral("Pill"));
        QVERIFY(!killedConfig.data().pillYoloRoute);
        // A save must not quietly re-enable it. (Snapshot first: save() writes through
        // data_, so handing it a reference to data_ would be self-aliasing.)
        const AppConfigData killedSnapshot = killedConfig.data();
        QVERIFY(killedConfig.save(killedSnapshot));
        const QJsonObject persisted =
            QJsonDocument::fromJson(
                [&] {
                    QFile file(QDir(killedRoot).filePath(QStringLiteral("settings.json")));
                    return file.open(QIODevice::ReadOnly) ? file.readAll() : QByteArray();
                }())
                .object();
        QVERIFY(persisted.contains(QStringLiteral("pill_yolo_route")));
        QCOMPARE(persisted.value(QStringLiteral("pill_yolo_route")), QJsonValue(false));
        AppConfig reloaded(killedRoot);
        QVERIFY(reloaded.load());
        QVERIFY(!reloaded.data().pillYoloRoute);

        // An existing install that predates the key comes up ON, which is what puts a
        // Pill user on the proposer that can see the meter without touching their file.
        const QString legacyRoot = QDir(temp.path()).filePath(QStringLiteral("legacy"));
        const QJsonObject legacy{{QStringLiteral("meter_style"), QStringLiteral("Pill")},
                                 {QStringLiteral("meter_proposer"), QStringLiteral("cv")}};
        QVERIFY(createExecutableFixture(
            QDir(legacyRoot).filePath(QStringLiteral("settings.json")),
            QJsonDocument(legacy).toJson(QJsonDocument::Compact)));
        AppConfig legacyConfig(legacyRoot);
        QVERIFY(legacyConfig.load());
        QVERIFY(legacyConfig.data().pillYoloRoute);

        // ...and the launch route reads exactly those two loaded fields.
        QProcessEnvironment env;
        seedProposerEnv(env, legacyConfig.data().meterProposer);
        const MeterStyleRouteResult route = applyPillYoloRoute(
            env, legacyConfig.data().meterStyle, legacyConfig.data().pillYoloRoute);
        QVERIFY(route.routed);
        QCOMPARE(env.value(QString::fromLatin1(kMeterProposerEnvKey)), QStringLiteral("yolo"));
        QCOMPARE(env.value(QString::fromLatin1(kMeterStyleEnvKey)), QStringLiteral("pill"));
    }
#endif

#ifndef ORION_PRODUCTION_BUILD
    void developmentConfigMigratesStalePathToActiveRepositoryImage()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString rootDir = QDir(temp.path()).filePath(QStringLiteral("active-repo"));
        const QString repository =
            remote_play_executable_policy::repositoryOrionStreamPath(rootDir);
        const QString external = QDir(temp.path()).filePath(QStringLiteral("old/OrionStream.exe"));
        QVERIFY(createExecutableFixture(repository, QByteArrayLiteral("repository")));
        QVERIFY(createExecutableFixture(external, QByteArrayLiteral("external")));

        const QJsonObject settings{{QStringLiteral("chiaki_path"), external}};
        QVERIFY(createExecutableFixture(
            QDir(rootDir).filePath(QStringLiteral("settings.json")),
            QJsonDocument(settings).toJson(QJsonDocument::Compact)));

        AppConfig config(rootDir);
        QVERIFY(config.load());
        QCOMPARE(config.data().chiakiPath, canonical(repository));
    }
#else
    void productionConfigNormalizesToPackagedIntent()
    {
#ifdef ORION_PRODUCTION_BUILD
        // Same reason as packetCaptureShipsOnAndRemainsOptOutable above: this fixture builds
        // a repo-shaped temporary root, which a production build's data-dir redirect ignores.
        QSKIP("AppConfig settings path redirects to the per-user data dir in production");
#endif
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString rootDir = QDir(temp.path()).filePath(QStringLiteral("repo"));
        const QString external = QDir(temp.path()).filePath(QStringLiteral("old/OrionStream.exe"));
        QVERIFY(createExecutableFixture(external, QByteArrayLiteral("external")));
        const QJsonObject settings{{QStringLiteral("chiaki_path"), external}};
        QVERIFY(createExecutableFixture(
            QDir(rootDir).filePath(QStringLiteral("settings.json")),
            QJsonDocument(settings).toJson(QJsonDocument::Compact)));

        AppConfig config(rootDir);
        QVERIFY(config.load());
        QCOMPARE(config.data().chiakiPath,
                 remote_play_executable_policy::packagedOrionStreamPath(
                     QCoreApplication::applicationDirPath()));
    }
#endif
};

QTEST_APPLESS_MAIN(RemotePlayExecutablePolicyTests)

#include "RemotePlayExecutablePolicyTests.moc"
