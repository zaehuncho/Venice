#include "AppConfig.h"
#include "PacketBridgeAuthority.h"
#include "RemotePlayExecutablePolicy.h"

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
