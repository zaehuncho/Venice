#include "AppConfig.h"
#include "PacketBridgeAuthority.h"
#include "RemotePlayExecutablePolicy.h"
#include "SidecarReaderProfile.h"
#include "SidecarWatchdog.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QHash>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QScopeGuard>
#include <QtCore/QSet>
#include <QtCore/QTemporaryDir>
#include <QtCore/QTimer>
#include <limits>
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
    void captureCardSelectionDefaultsUnselectedAndRoundTrips()
    {
        AppConfigData data;
        QVERIFY(data.captureCardDeviceId.isEmpty());
#ifndef ORION_PRODUCTION_BUILD
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        AppConfig writer(temp.path());
        data.captureCardIndex = 1;
        data.captureCardDeviceId = QStringLiteral("dshow-moniker-sha256-v1:")
            + QString(64, QLatin1Char('2'));
        QString error;
        QVERIFY2(writer.save(data, &error), qPrintable(error));
        AppConfig reader(temp.path());
        QVERIFY(reader.load());
        QCOMPARE(reader.data().captureCardIndex, 1);
        QCOMPARE(reader.data().captureCardDeviceId, data.captureCardDeviceId);
#endif
    }

    void sourceSwitchRefusesActiveAndTeardownEventLoop()
    {
        bool producerCapture = true;
        int appliedLead = 8;
        const auto trySwitch = [&](bool connecting, bool running, bool disconnecting) {
            if (videoSourceChangeAllowed(connecting, running, disconnecting)) {
                producerCapture = false;
                appliedLead = 29;
            }
        };
        for (const auto state : {1, 2, 3}) {
            bool fired = false;
            QTimer::singleShot(1, this, [&] {
                trySwitch(state == 1, state == 2, state == 3);
                fired = true;
            });
            QTRY_VERIFY_WITH_TIMEOUT(fired, 1000);
            QVERIFY(producerCapture);
            QCOMPARE(appliedLead, 8);
        }
        trySwitch(false, false, false);
        QVERIFY(!producerCapture);
        QCOMPARE(appliedLead, 29);
    }

    void warmPreviewPromotionRequiresExactLaunchIdentityEventLoop()
    {
        AppConfigData warm;
        warm.videoSource = QStringLiteral("capture_card");
        warm.captureCardIndex = 0;
        warm.captureCardDeviceId = QStringLiteral("A");
        warm.captureCardFps = 60;
        AppConfigData requested = warm;
        bool promoted = false;
        bool fired = false;
        QTimer::singleShot(1, this, [&] {
            requested.captureCardIndex = 1;
            requested.captureCardDeviceId = QStringLiteral("B");
            promoted = shouldReuseWarmSidecarForConnect(true, true, true,
                captureSidecarLaunchIdentity(warm), captureSidecarLaunchIdentity(requested));
            fired = true;
        });
        QTRY_VERIFY_WITH_TIMEOUT(fired, 1000);
        QVERIFY(!promoted);
        requested = warm;
        requested.captureCardFps = 30;
        QVERIFY(!shouldReuseWarmSidecarForConnect(true, true, true,
            captureSidecarLaunchIdentity(warm), captureSidecarLaunchIdentity(requested)));
        requested = warm;
        QVERIFY(shouldReuseWarmSidecarForConnect(true, true, true,
            captureSidecarLaunchIdentity(warm), captureSidecarLaunchIdentity(requested)));
    }

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
        // [ORION_PILL_REMOVED, re-withdrawn 2026-09-23] A persisted "Pill" is MIGRATED to Arrow2 on
        // load; the kill-switch key still round-trips untouched.
        QCOMPARE(killedConfig.data().meterStyle, QStringLiteral("Arrow2"));
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

        // [ORION_PILL_REMOVED, re-withdrawn 2026-09-23] A persisted "Pill" loads as Arrow2, so the
        // Pill -> yolo route is unreachable from a settings file (env untouched, no style key).
        QCOMPARE(legacyConfig.data().meterStyle, QStringLiteral("Arrow2"));
        QProcessEnvironment env;
        seedProposerEnv(env, legacyConfig.data().meterProposer);
        const MeterStyleRouteResult route = applyPillYoloRoute(
            env, legacyConfig.data().meterStyle, legacyConfig.data().pillYoloRoute);
        QVERIFY(!route.routed);
        QVERIFY(route.log.isEmpty());
        QCOMPARE(env.value(QString::fromLatin1(kMeterProposerEnvKey)), QStringLiteral("cv"));
        QVERIFY(!env.contains(QString::fromLatin1(kMeterStyleEnvKey)));
    }

    // [SHIP_PARITY R1-R3 2026-09-23] The native timing profile carries the owner's two dev-launch
    // timing pins into every production process: ORION_TIP_PHASE_SOLO=1 and
    // ORION_CURVE_STRETCH_ALPHA=0, under the bumped profile id. Production overrides an inherited
    // value; development preserves an explicit one and fills silence with the profile.
    void nativeTimingProfilePinsSoloAndStretchInProduction()
    {
        const QByteArray keys[] = {"ORION_TIP_PHASE_SOLO", "ORION_CURVE_STRETCH_ALPHA",
                                   "ORION_HORIZON_DEBIAS", "ORION_RAMP_SHAPE",
                                   "ORION_TIMING_PROFILE_ID"};
        QHash<QByteArray, QByteArray> saved;
        QSet<QByteArray> wasSet;
        for (const QByteArray& k : keys) {
            if (qEnvironmentVariableIsSet(k.constData())) {
                wasSet.insert(k);
                saved.insert(k, qgetenv(k.constData()));
            }
        }
        const auto restore = qScopeGuard([&] {
            for (const QByteArray& k : keys) {
                if (wasSet.contains(k)) {
                    qputenv(k.constData(), saved.value(k));
                } else {
                    qunsetenv(k.constData());
                }
            }
        });

        // Production: hostile inherited values lose.
        qputenv("ORION_TIP_PHASE_SOLO", "0");
        qputenv("ORION_CURVE_STRETCH_ALPHA", "0.6");
        const QString production = applyShippedNativeTimingProfile(false);
        QCOMPARE(qgetenv("ORION_TIP_PHASE_SOLO"), QByteArray("1"));
        QCOMPARE(qgetenv("ORION_CURVE_STRETCH_ALPHA"), QByteArray("0"));
        QCOMPARE(qgetenv("ORION_TIMING_PROFILE_ID"), QByteArray("2k27-2026-09-23-v3"));
        QVERIFY2(production.contains(QStringLiteral("ORION_TIP_PHASE_SOLO=1(production)")),
                 qPrintable(production));
        QVERIFY2(production.contains(QStringLiteral("ORION_CURVE_STRETCH_ALPHA=0(production)")),
                 qPrintable(production));
        QVERIFY(production.startsWith(QStringLiteral("id=2k27-2026-09-23-v3")));

        // Development: an explicit operator value survives (A/B work).
        qputenv("ORION_TIP_PHASE_SOLO", "0");
        qputenv("ORION_CURVE_STRETCH_ALPHA", "0.6");
        const QString dev = applyShippedNativeTimingProfile(true);
        QCOMPARE(qgetenv("ORION_TIP_PHASE_SOLO"), QByteArray("0"));
        QCOMPARE(qgetenv("ORION_CURVE_STRETCH_ALPHA"), QByteArray("0.6"));
        QVERIFY(dev.contains(QStringLiteral("ORION_CURVE_STRETCH_ALPHA=0.6(env)")));

        // Development with silence: the profile fills it.
        qunsetenv("ORION_TIP_PHASE_SOLO");
        qunsetenv("ORION_CURVE_STRETCH_ALPHA");
        const QString silent = applyShippedNativeTimingProfile(true);
        QCOMPARE(qgetenv("ORION_TIP_PHASE_SOLO"), QByteArray("1"));
        QCOMPARE(qgetenv("ORION_CURVE_STRETCH_ALPHA"), QByteArray("0"));
        QVERIFY(silent.contains(QStringLiteral("ORION_TIP_PHASE_SOLO=1(profile)")));
    }

    // [ORION_LEFT_FADE_LATER 2026-09-22] The old +8 left-fade default is migrated to -6 exactly once:
    // a pre-revision file holding +8 adopts -6; any other value is a deliberate choice and is kept;
    // after a save (rev 2) even a deliberate +8 survives.
    void leftFadeOffsetMigratesOldDefaultOnce()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const auto loadWith = [&](const QString& name, const QJsonObject& obj) {
            const QString root = QDir(temp.path()).filePath(name);
            if (!createExecutableFixture(QDir(root).filePath(QStringLiteral("settings.json")),
                                         QJsonDocument(obj).toJson(QJsonDocument::Compact)))
                return std::numeric_limits<double>::quiet_NaN();
            AppConfig cfg(root);
            if (!cfg.load())
                return std::numeric_limits<double>::quiet_NaN();
            return cfg.data().leadOffsetLeftFadeMs;
        };
        QCOMPARE(loadWith(QStringLiteral("old8"),
                          QJsonObject{{QStringLiteral("lead_offset_left_fade_ms"), 8.0}}), -6.0);
        QCOMPARE(loadWith(QStringLiteral("deliberate3"),
                          QJsonObject{{QStringLiteral("lead_offset_left_fade_ms"), 3.0}}), 3.0);
        QCOMPARE(loadWith(QStringLiteral("rev2_8"),
                          QJsonObject{{QStringLiteral("lead_offset_left_fade_ms"), 8.0},
                                      {QStringLiteral("lead_offset_left_fade_rev"), 2}}), 8.0);
        QCOMPARE(loadWith(QStringLiteral("absent"),
                          QJsonObject{{QStringLiteral("meter_style"), QStringLiteral("Arrow2")}}), -6.0);
    }

    // [ORION_PILL_BETA 2026-09-22] The generic save API is the fourth ingress (Astra):
    // normalizedMeterStyle() runs on save as well as load. Pill is accepted again; an unknown
    // style still persists as the default.
    void savePersistsPillBetaAndDefaultsUnknownStyles()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        AppConfig cfg(temp.path());
        AppConfigData data;
        data.meterStyle = QStringLiteral("Pill");
        QVERIFY(cfg.save(data));
        const QJsonObject persisted =
            QJsonDocument::fromJson(
                [&] {
                    QFile file(QDir(temp.path()).filePath(QStringLiteral("settings.json")));
                    return file.open(QIODevice::ReadOnly) ? file.readAll() : QByteArray();
                }())
                .object();
        QCOMPARE(persisted.value(QStringLiteral("meter_style")).toString(), QStringLiteral("Arrow2"));
        AppConfig reloaded(temp.path());
        QVERIFY(reloaded.load());
        QCOMPARE(reloaded.data().meterStyle, QStringLiteral("Arrow2"));
        // The rule itself. [RT-LOW-06 / CL3-F4-003 2026-09-23] The accepted set IS the offered
        // set: the UI offers Arrow2 only, so every legacy 2K26 profile (Arrow, Dial, Straight,
        // Sword), Pill, an unknown value and any spelling of arrow2 all land on "Arrow2".
        // (Before the fix "  Straight " survived as "Straight" -- this row failed.)
        for (const QString& legacy : {QStringLiteral("  Straight "), QStringLiteral("Straight"),
                                      QStringLiteral("Arrow"), QStringLiteral("dial"),
                                      QStringLiteral("SWORD"), QStringLiteral("pill"),
                                      QStringLiteral("Mystery"), QString(),
                                      QStringLiteral("arrow2"), QStringLiteral(" Arrow2 ")}) {
            QCOMPARE(normalizedMeterStyle(legacy), QStringLiteral("Arrow2"));
        }
    }

    // [RT-LOW-06 2026-09-23] A stored legacy style migrates on LOAD (not only on save), and the
    // next save persists the canonical value.
    void loadMigratesLegacyMeterStylesToArrow2()
    {
        for (const QString& legacy : {QStringLiteral("Straight"), QStringLiteral("Dial"),
                                      QStringLiteral("Sword"), QStringLiteral("Arrow"),
                                      QStringLiteral("Pill"), QStringLiteral("Mystery")}) {
            QTemporaryDir temp;
            QVERIFY(temp.isValid());
            {
                QFile file(QDir(temp.path()).filePath(QStringLiteral("settings.json")));
                QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::Truncate));
                file.write(QJsonDocument(QJsonObject{{QStringLiteral("meter_style"), legacy}})
                               .toJson(QJsonDocument::Compact));
            }
            AppConfig cfg(temp.path());
            QVERIFY(cfg.load());
            QCOMPARE(cfg.data().meterStyle, QStringLiteral("Arrow2"));
            QVERIFY(cfg.save(cfg.data()));
            QFile file(QDir(temp.path()).filePath(QStringLiteral("settings.json")));
            QVERIFY(file.open(QIODevice::ReadOnly));
            QCOMPARE(QJsonDocument::fromJson(file.readAll()).object()
                         .value(QStringLiteral("meter_style")).toString(),
                     QStringLiteral("Arrow2"));
        }
    }
#endif

    // [RT-MED-01 / CL3-F4-009 2026-09-23] Calibration Cancel restores the EXACT pre-calibration
    // tuple. Before the fix the controller restored through setActuationLeadMs(), which is what
    // `stepLikeSetActuationLeadMs` models: an Auto install (0,false) came back as (150,true).
    void leadCalibrationCancelRestoresExactProvenance()
    {
        const auto stepLikeSetActuationLeadMs = [](AppConfigData& d, double v) {
            d.actuationLeadMs = qBound(AppConfigData::kActuationLeadMinMs, v,
                                       AppConfigData::kActuationLeadMaxMs);
            d.actuationLeadUserSet = true;
            mirrorActuationLeadIntoSourceStash(d);
        };
        struct Case { const char* name; double lead; bool userSet; };
        const Case cases[] = {
            {"auto_zero", 0.0, false},         // fresh install: Auto
            {"measured_seed", 270.0, false},   // measured seed, never touched
            {"user_set", 280.0, true},         // the customer's own value
        };
        for (const Case& c : cases) {
            AppConfigData data;
            data.videoSource = QStringLiteral("capture_card");
            data.actuationLeadMs = c.lead;
            data.actuationLeadUserSet = c.userSet;
            mirrorActuationLeadIntoSourceStash(data);
            const AppConfigData before = data;
            const ActuationLeadProvenance snap = captureActuationLeadProvenance(data);

            // Two graded calibration steps persisted through the user setter (LATE then GOOD
            // walking to 320), exactly like reportLeadCalibrationVerdict does.
            stepLikeSetActuationLeadMs(data, 320.0);
            QVERIFY(data.actuationLeadUserSet);

            QCOMPARE(restoreActuationLeadProvenance(data, snap), ActuationLeadRestore::Restored);
            QVERIFY2(data.actuationLeadMs == before.actuationLeadMs,
                     qPrintable(QStringLiteral("%1: lead %2 != %3").arg(QLatin1String(c.name))
                                    .arg(data.actuationLeadMs).arg(before.actuationLeadMs)));
            QVERIFY2(data.actuationLeadUserSet == before.actuationLeadUserSet, c.name);
            QCOMPARE(data.actuationLeadBySourceMs, before.actuationLeadBySourceMs);
            QCOMPARE(data.actuationLeadUserSetBySource, before.actuationLeadUserSetBySource);
            // A second Cancel-restore is a no-op.
            QCOMPARE(restoreActuationLeadProvenance(data, snap), ActuationLeadRestore::Unchanged);
        }
        // Auto stays Auto: no stash entry for the route (absent == not configured) and the
        // seeding rule still accepts a measured seed afterwards.
        {
            AppConfigData data;
            const ActuationLeadProvenance snap = captureActuationLeadProvenance(data);
            stepLikeSetActuationLeadMs(data, 0.0);   // clamped UP to 150, user_set latched
            QCOMPARE(data.actuationLeadMs, AppConfigData::kActuationLeadMinMs);
            QCOMPARE(restoreActuationLeadProvenance(data, snap), ActuationLeadRestore::Restored);
            QCOMPARE(data.actuationLeadMs, 0.0);
            QVERIFY(!data.actuationLeadUserSet);
            QVERIFY(!data.actuationLeadBySourceMs.contains(actuationLeadRouteKey(data)));
            QVERIFY(actuationLeadAcceptsMeasuredSeed(data));
            QVERIFY(describeActuationLeadProvenance(snap).startsWith(QStringLiteral("Auto")));
        }
        // A route switch mid-calibration: nothing is written onto the other route.
        {
            AppConfigData data;
            data.videoSource = QStringLiteral("capture_card");
            data.actuationLeadMs = 280.0;
            data.actuationLeadUserSet = true;
            mirrorActuationLeadIntoSourceStash(data);
            const ActuationLeadProvenance snap = captureActuationLeadProvenance(data);
            data.videoSource = QStringLiteral("decoder");
            data.actuationLeadMs = 310.0;
            const AppConfigData switched = data;
            QCOMPARE(restoreActuationLeadProvenance(data, snap),
                     ActuationLeadRestore::RouteChanged);
            QCOMPARE(data.actuationLeadMs, switched.actuationLeadMs);
            QCOMPARE(data.actuationLeadUserSet, switched.actuationLeadUserSet);
        }
        QVERIFY(describeActuationLeadProvenance({270.0, false, QString()})
                    .contains(QStringLiteral("measured")));
        QVERIFY(describeActuationLeadProvenance({280.0, true, QString()})
                    .contains(QStringLiteral("your value")));
    }

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

QTEST_GUILESS_MAIN(RemotePlayExecutablePolicyTests)

#include "RemotePlayExecutablePolicyTests.moc"
