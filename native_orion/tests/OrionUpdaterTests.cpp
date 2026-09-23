#include "../src/Diagnostics.h"
#include "../src/Ed25519.h"
#include "../src/LicenseClient.h"     // compareSemanticVersions
#include "../src/UpdateManifest.h"
#include "../src/UpdaterArchive.h"
#include "../src/UpdaterTrust.h"
#include <QtCore/QDirIterator>
#include <QtCore/QProcess>
#if defined(Q_OS_WIN)
#include <Windows.h>
#include <winioctl.h>
#endif

#include <QtTest/QtTest>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QTemporaryDir>

#include <QtCore/private/qzipwriter_p.h>

using namespace orion;
using namespace orion::updater;

namespace {

// Deterministic 32-byte Ed25519 test seed (not a real key). The matching public
// key is derived at runtime via OpenSSL so tests need no memorized vectors.
QByteArray testSeed()
{
    QByteArray seed(32, Qt::Uninitialized);
    for (int i = 0; i < 32; ++i) {
        seed[i] = static_cast<char>(0x10 + i);
    }
    return seed;
}

// Sign the manifest's canonical bytes with the test seed and store the hex sig.
void signManifestEd25519(UpdateManifest& m, const QByteArray& seed)
{
    m.signature = QString::fromLatin1(ed25519Sign(seed, canonicalManifestSigningString(m)).toHex());
}

UpdateManifest sampleManifest()
{
    UpdateManifest m;
    m.ok = true;
    m.version = QStringLiteral("1.0.4");
    m.minVersion = QStringLiteral("1.0.0");
    m.url = QStringLiteral("https://updates.zaeorion.com/orion-1.0.4.zip");
    m.sha256 = QStringLiteral("a") + QString(63, QLatin1Char('b'));
    m.signatureAlg = QStringLiteral("ed25519");
    m.publicKeyId = QStringLiteral("orion-test-1");
    m.publishedAt = QStringLiteral("2026-06-09T00:00:00Z");
    m.mandatory = false;
    m.allowRollback = false;
    return m;
}

bool writeTextFile(const QString& path, const QByteArray& contents)
{
    QFile f(path);
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return false;
    }
    f.write(contents);
    return true;
}

quint32 crc32Of(const QByteArray& data)
{
    static quint32 table[256];
    static bool init = false;
    if (!init) {
        for (quint32 i = 0; i < 256; ++i) {
            quint32 c = i;
            for (int k = 0; k < 8; ++k) {
                c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            }
            table[i] = c;
        }
        init = true;
    }
    quint32 crc = 0xFFFFFFFFu;
    for (char b : data) {
        crc = table[(crc ^ static_cast<quint8>(b)) & 0xFFu] ^ (crc >> 8);
    }
    return crc ^ 0xFFFFFFFFu;
}

void putLE16(QByteArray& a, quint16 v)
{
    a.append(static_cast<char>(v & 0xFF));
    a.append(static_cast<char>((v >> 8) & 0xFF));
}

void putLE32(QByteArray& a, quint32 v)
{
    a.append(static_cast<char>(v & 0xFF));
    a.append(static_cast<char>((v >> 8) & 0xFF));
    a.append(static_cast<char>((v >> 16) & 0xFF));
    a.append(static_cast<char>((v >> 24) & 0xFF));
}

// QZipWriter sanitizes "../" out of entry names, so it cannot author a malicious
// archive. We hand-craft a minimal single-entry (stored, uncompressed) zip whose
// central-directory name is preserved verbatim, which is what a real attacker
// would ship.
bool writeStoredZip(const QString& path, const QString& entryName, const QByteArray& data)
{
    const QByteArray name = entryName.toUtf8();
    const quint32 crc = crc32Of(data);
    const quint32 dataLen = static_cast<quint32>(data.size());
    const quint16 nameLen = static_cast<quint16>(name.size());

    QByteArray zip;
    // Local file header
    putLE32(zip, 0x04034b50);
    putLE16(zip, 20); putLE16(zip, 0); putLE16(zip, 0); putLE16(zip, 0); putLE16(zip, 0);
    putLE32(zip, crc); putLE32(zip, dataLen); putLE32(zip, dataLen);
    putLE16(zip, nameLen); putLE16(zip, 0);
    zip.append(name);
    zip.append(data);

    const quint32 cdOffset = static_cast<quint32>(zip.size());
    QByteArray cd;
    putLE32(cd, 0x02014b50);
    putLE16(cd, 20); putLE16(cd, 20); putLE16(cd, 0); putLE16(cd, 0); putLE16(cd, 0); putLE16(cd, 0);
    putLE32(cd, crc); putLE32(cd, dataLen); putLE32(cd, dataLen);
    putLE16(cd, nameLen); putLE16(cd, 0); putLE16(cd, 0); putLE16(cd, 0); putLE16(cd, 0);
    putLE32(cd, 0); putLE32(cd, 0);
    cd.append(name);
    zip.append(cd);

    // End of central directory
    putLE32(zip, 0x06054b50);
    putLE16(zip, 0); putLE16(zip, 0); putLE16(zip, 1); putLE16(zip, 1);
    putLE32(zip, static_cast<quint32>(cd.size())); putLE32(zip, cdOffset);
    putLE16(zip, 0);

    QFile f(path);
    if (!f.open(QIODevice::WriteOnly)) {
        return false;
    }
    return f.write(zip) == zip.size();
}

} // namespace

class OrionUpdaterTests final : public QObject {
    Q_OBJECT
private slots:
    // --- semantic version comparison ---
    void semanticVersionComparisonOrdersBuilds();

    // --- manifest JSON parsing ---
    void parsesWellFormedManifest();
    void parseRejectsMalformedJson();
    void parseRejectsMissingVersion();
    void parseToleratesFieldNameVariants();
    void parseHonoursOkFalseEnvelope();

    // --- Ed25519 signature / hash verification ---
    void canonicalMatchesServerBytes();
    void verifiesLiveBackendManifestVector();
    void ed25519PrimitiveRoundTrips();
    void signatureVerifiesWithCorrectKey();
    void signatureRejectsTamperedFields();
    void signatureRejectsWrongPublicKey();
    void signatureRejectsMissingPublicKeyId();
    void signatureRejectsUnsupportedAlg();
    void artifactHashVerifies();
    void artifactHashRejectsMismatchAndBadLength();

    // --- update / downgrade policy ---
    void evaluateReportsUpToDateAndAvailable();
    void evaluateFlagsMandatoryUpdate();
    void evaluateRefusesDowngradeUnlessRollbackAllowed();
    void evaluateBlocksClientBelowMinimum();

    // --- release rings + startup gate policy ---
    void parseReadsChannelField();
    void channelDoesNotAffectSignature();
    void gatePolicyMatrix();

    // --- diagnostics-export redaction ---
    void redactionStripsSecretsKeepsTelemetry();

    // --- path traversal rejection in zip extraction ---
    void safeJoinAcceptsDescendants();
    void safeJoinRejectsTraversalAndAbsolute();
    void extractRejectsPathTraversalZip();
    void extractRejectsSymlinkZip();
    void extractsCleanZip();

    // --- backup / replace / rollback ---
    void backupRestoreRoundTrip();
    void applyTreeOverwritesAndAdds();
    void failedApplyRollsBackToBackup();
    // [2026-09-21] trust root + reparse points
    void trustRootProductionTrustsOnlyEmbeddedKey();
    void trustRootDevResolvesExternalSourcesInOrder();
    void localManifestRefusedInProduction();
    void reparsePointDetectedUnderRoot();
    void applyTreeRefusesJunctionInInstallDir();
    void backupTreeRefusesJunctionInInstallDir();
    // [2026-09-21 Codex round 2]
    void trustRootProductionAcceptsEveryEmbeddedKeyAndNothingElse();
    void installRootBindingRefusesReparseRootAndForeignDirInProduction();
    void rootJunctionIsRefusedForEveryWrite();
    void applyTreeDoesNotCreateDirectoriesThroughAJunction();
    void restoreTreeIsExactAfterAPartialApply();
    void applyTreeOverwritingHardlinkLeavesTheOtherLinkIntact();
    // [2026-09-21 Codex round 3]
    void rollbackPreservesTheLiveUpdaterLog();
    void reparseLookupFailsClosedAndHandlesLongPaths();
    void buildProfileOutputIsConfinedToTemp();
    // [2026-09-21 Codex round 4]
    void relaunchNameIsPinnedInProductionAndSanitisedInDev();
    void safeRelaunchPathRefusesLinksAndEscapes();
    void extractRefusesPreexistingJunctionStagingRoot();
    void backupRefusesPreexistingJunctionBackupRoot();
    void restoreTreeIsExactWhenThePlantedJunctionIsStillThere();
    // [2026-09-23 A6] Executing from a verified sibling closure and manifest delta.
    void stagedRuntimeClosureIsCompleteAndOutsideInstall();
    void retiredManifestedFilesArePrunedButUserDataSurvivesRollback();
    // [2026-09-21 Codex round 5]
    void removeTreeSafelyUnlinksAChildJunctionWithoutFollowing();
    void extractAndBackupClearRootsWithoutFollowingChildJunctions();
    void restoreReplacesAJunctionThatShadowsABackedUpDirectory();
    // [2026-09-21 Codex round 6]
    void extendedLengthPathHandlesDriveAndUncForms();
    void extractRefusesTruncatedEntry();
    void restoreReplacesAFileSymlinkThatShadowsABackedUpFile();
};

void OrionUpdaterTests::semanticVersionComparisonOrdersBuilds()
{
    QCOMPARE(compareSemanticVersions(QStringLiteral("1.0.0"), QStringLiteral("1.0.1")), -1);
    QCOMPARE(compareSemanticVersions(QStringLiteral("1.2.0"), QStringLiteral("1.1.9")), 1);
    QCOMPARE(compareSemanticVersions(QStringLiteral("1.0.3"), QStringLiteral("1.0.3")), 0);
    // shorter strings are zero-padded
    QCOMPARE(compareSemanticVersions(QStringLiteral("1.0"), QStringLiteral("1.0.0")), 0);
    QCOMPARE(compareSemanticVersions(QStringLiteral("2"), QStringLiteral("1.9.9")), 1);
}

void OrionUpdaterTests::parsesWellFormedManifest()
{
    const QByteArray payload = R"({
        "ok": true,
        "version": "1.0.4",
        "minimum_supported_version": "1.0.0",
        "url": "https://updates.zaeorion.com/orion-1.0.4.zip",
        "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        "signature": "deadbeef",
        "signature_alg": "ed25519",
        "public_key_id": "orion-2026-1",
        "published_at": "2026-06-09T00:00:00Z",
        "mandatory": true,
        "allow_rollback": false,
        "notes": "Bug fixes"
    })";
    const UpdateManifest m = parseUpdateManifest(payload);
    QVERIFY(m.ok);
    QCOMPARE(m.version, QStringLiteral("1.0.4"));
    QCOMPARE(m.minVersion, QStringLiteral("1.0.0"));
    QCOMPARE(m.url, QStringLiteral("https://updates.zaeorion.com/orion-1.0.4.zip"));
    QCOMPARE(m.sha256.size(), 64);
    QCOMPARE(m.signatureAlg, QStringLiteral("ed25519"));
    QCOMPARE(m.publicKeyId, QStringLiteral("orion-2026-1"));
    QCOMPARE(m.publishedAt, QStringLiteral("2026-06-09T00:00:00Z"));
    QVERIFY(m.mandatory);
    QVERIFY(!m.allowRollback);
    QCOMPARE(m.notes, QStringLiteral("Bug fixes"));
}

void OrionUpdaterTests::parseRejectsMalformedJson()
{
    const UpdateManifest m = parseUpdateManifest(QByteArrayLiteral("{not json"));
    QVERIFY(!m.ok);
    QVERIFY(!m.message.isEmpty());
}

void OrionUpdaterTests::parseRejectsMissingVersion()
{
    const UpdateManifest m = parseUpdateManifest(QByteArrayLiteral(R"({"ok":true,"url":"https://x/y.zip"})"));
    QVERIFY(!m.ok);
}

void OrionUpdaterTests::parseToleratesFieldNameVariants()
{
    const QByteArray payload = R"({
        "latest": "1.1.0",
        "min_version": "1.0.5",
        "download_url": "https://updates.zaeorion.com/orion-1.1.0.zip",
        "artifact_sha256": "AABBCC",
        "signature": "FEEDFACE",
        "required": true,
        "allow_downgrade": true
    })";
    const UpdateManifest m = parseUpdateManifest(payload);
    QVERIFY(m.ok);
    QCOMPARE(m.version, QStringLiteral("1.1.0"));
    QCOMPARE(m.minVersion, QStringLiteral("1.0.5"));
    QCOMPARE(m.url, QStringLiteral("https://updates.zaeorion.com/orion-1.1.0.zip"));
    QCOMPARE(m.sha256, QStringLiteral("AABBCC")); // stored verbatim (signed value is exact)
    QCOMPARE(m.signature, QStringLiteral("FEEDFACE"));
    QVERIFY(m.mandatory);
    QVERIFY(m.allowRollback);
}

void OrionUpdaterTests::parseHonoursOkFalseEnvelope()
{
    const UpdateManifest m = parseUpdateManifest(
        QByteArrayLiteral(R"({"ok":false,"error":"maintenance","version":"9.9.9"})"));
    QVERIFY(!m.ok);
    QCOMPARE(m.message, QStringLiteral("maintenance"));
}

namespace {
// The live v0.4.0 backend manifest, its pinned public key, and the genuine
// signature captured from https://api.zaeorion.com/api/update. This is the gold
// cross-check: if the client's canonicalization or verification drifts a single
// byte from the deployed signer, these two tests fail.
UpdateManifest liveBackendManifest()
{
    UpdateManifest m;
    m.ok = true;
    m.version = QStringLiteral("0.1.0");
    m.minVersion = QStringLiteral("0.1.0");
    m.url = QStringLiteral("https://orion-artifacts-987622176566.s3.amazonaws.com/orion-launcher-0.1.0.exe");
    m.sha256 = QStringLiteral("abc123placeholder000000000000000000000000000000000000000000000000");
    m.publishedAt = QStringLiteral("2026-06-10T05:53:09Z");
    m.mandatory = false;
    m.allowRollback = false;
    m.publicKeyId = QStringLiteral("orion-ed25519-v1");
    m.signatureAlg = QStringLiteral("ed25519");
    m.signature = QStringLiteral("HgeY4xkhU2ToBqHRfBw-ykaAHYykOUAW4g8Q2u-VgnywOgE8qefHoCHijtiLmm3685qyCwJCAnX0tN_yTKd_AQ");
    return m;
}
const QByteArray kLivePublicKeyB64 = QByteArrayLiteral("OJQ2E7ZAFClOCM4S4/5QzLeQjjEMSZjPiGMzbiLZdWs=");
} // namespace

void OrionUpdaterTests::canonicalMatchesServerBytes()
{
    const QByteArray expected =
        "{\"latest_version\":\"0.1.0\",\"minimum_supported_version\":\"0.1.0\","
        "\"artifact_url\":\"https://orion-artifacts-987622176566.s3.amazonaws.com/orion-launcher-0.1.0.exe\","
        "\"sha256\":\"abc123placeholder000000000000000000000000000000000000000000000000\","
        "\"published_at\":\"2026-06-10T05:53:09Z\",\"mandatory\":false,\"allow_rollback\":false,"
        "\"public_key_id\":\"orion-ed25519-v1\"}";
    QCOMPARE(canonicalManifestSigningString(liveBackendManifest()), expected);
}

void OrionUpdaterTests::verifiesLiveBackendManifestVector()
{
    const QByteArray pub = decodeEd25519PublicKey(QString::fromLatin1(kLivePublicKeyB64));
    QCOMPARE(pub.size(), 32);
    // Sanity: the pinned key decodes to the documented hex.
    QCOMPARE(QString::fromLatin1(pub.toHex()),
             QStringLiteral("38943613b64014294e08ce12e3fe50ccb7908e310c4998cf8863336e22d9756b"));
    QVERIFY(verifyManifestSignature(liveBackendManifest(), pub));

    // Tampering any signed field must break verification of the real signature.
    UpdateManifest tampered = liveBackendManifest();
    tampered.url = QStringLiteral("https://evil.example.com/x.exe");
    QVERIFY(!verifyManifestSignature(tampered, pub));
}

void OrionUpdaterTests::ed25519PrimitiveRoundTrips()
{
    QVERIFY2(ed25519Available(), "libcrypto with Ed25519 must be loadable for the updater");
    const QByteArray seed = testSeed();
    const QByteArray pub = ed25519DerivePublicKey(seed);
    QCOMPARE(pub.size(), 32);
    const QByteArray msg = QByteArrayLiteral("orion manifest bytes");
    const QByteArray sig = ed25519Sign(seed, msg);
    QCOMPARE(sig.size(), 64);
    QVERIFY(ed25519Verify(pub, msg, sig));
    // A one-byte tamper of the message must fail.
    QVERIFY(!ed25519Verify(pub, QByteArrayLiteral("orion manifest byteS"), sig));
}

void OrionUpdaterTests::signatureVerifiesWithCorrectKey()
{
    const QByteArray pub = ed25519DerivePublicKey(testSeed());
    QCOMPARE(pub.size(), 32);
    UpdateManifest m = sampleManifest();
    signManifestEd25519(m, testSeed());
    QVERIFY(verifyManifestSignature(m, pub));
}

void OrionUpdaterTests::signatureRejectsTamperedFields()
{
    const QByteArray pub = ed25519DerivePublicKey(testSeed());

    // Each tamper keeps the original signature but changes a signed field.
    {
        UpdateManifest m = sampleManifest();
        signManifestEd25519(m, testSeed());
        m.url = QStringLiteral("https://evil.example.com/payload.zip");
        QVERIFY(!verifyManifestSignature(m, pub));
    }
    {
        UpdateManifest m = sampleManifest();
        signManifestEd25519(m, testSeed());
        m.sha256 = QString(64, QLatin1Char('f'));
        QVERIFY(!verifyManifestSignature(m, pub));
    }
    {
        UpdateManifest m = sampleManifest();
        signManifestEd25519(m, testSeed());
        m.version = QStringLiteral("9.9.9");
        QVERIFY(!verifyManifestSignature(m, pub));
    }
    {
        UpdateManifest m = sampleManifest();
        signManifestEd25519(m, testSeed());
        m.allowRollback = !m.allowRollback;
        QVERIFY(!verifyManifestSignature(m, pub));
    }
}

void OrionUpdaterTests::signatureRejectsWrongPublicKey()
{
    UpdateManifest m = sampleManifest();
    signManifestEd25519(m, testSeed());

    // A different seed -> different public key -> verification fails.
    QByteArray otherSeed(32, Qt::Uninitialized);
    for (int i = 0; i < 32; ++i) {
        otherSeed[i] = static_cast<char>(0x99 - i);
    }
    const QByteArray wrongPub = ed25519DerivePublicKey(otherSeed);
    QCOMPARE(wrongPub.size(), 32);
    QVERIFY(!verifyManifestSignature(m, wrongPub));

    // An empty key (e.g. an unknown public_key_id that resolved to nothing) fails.
    QVERIFY(!verifyManifestSignature(m, QByteArray()));
}

void OrionUpdaterTests::signatureRejectsMissingPublicKeyId()
{
    const QByteArray pub = ed25519DerivePublicKey(testSeed());
    UpdateManifest m = sampleManifest();
    m.publicKeyId.clear();
    signManifestEd25519(m, testSeed());
    QVERIFY(!verifyManifestSignature(m, pub));
}

void OrionUpdaterTests::signatureRejectsUnsupportedAlg()
{
    const QByteArray pub = ed25519DerivePublicKey(testSeed());
    UpdateManifest m = sampleManifest();
    signManifestEd25519(m, testSeed());
    // Valid signature bytes, but the manifest declares a non-ed25519 algorithm.
    m.signatureAlg = QStringLiteral("hmac-sha256");
    QVERIFY(!verifyManifestSignature(m, pub));

    UpdateManifest empty = sampleManifest();
    signManifestEd25519(empty, testSeed());
    empty.signatureAlg.clear();
    QVERIFY(!verifyManifestSignature(empty, pub));
}

void OrionUpdaterTests::artifactHashVerifies()
{
    const QByteArray data = QByteArrayLiteral("orion update payload");
    const QString hex = QString::fromLatin1(QCryptographicHash::hash(data, QCryptographicHash::Sha256).toHex());
    QVERIFY(verifyArtifactSha256(data, hex));
    QVERIFY(verifyArtifactSha256(data, hex.toUpper())); // case-insensitive
}

void OrionUpdaterTests::artifactHashRejectsMismatchAndBadLength()
{
    const QByteArray data = QByteArrayLiteral("orion update payload");
    const QByteArray other = QByteArrayLiteral("tampered payload");
    const QString hex = QString::fromLatin1(QCryptographicHash::hash(other, QCryptographicHash::Sha256).toHex());
    QVERIFY(!verifyArtifactSha256(data, hex));
    QVERIFY(!verifyArtifactSha256(data, QStringLiteral("deadbeef")));   // wrong length
    QVERIFY(!verifyArtifactSha256(data, QString()));                    // empty
}

void OrionUpdaterTests::evaluateReportsUpToDateAndAvailable()
{
    UpdateManifest m = sampleManifest();
    QCOMPARE(evaluateUpdate(QStringLiteral("1.0.4"), m), UpdateDecision::UpToDate);
    QCOMPARE(evaluateUpdate(QStringLiteral("1.0.3"), m), UpdateDecision::UpdateAvailable);
    QCOMPARE(evaluateUpdate(QStringLiteral("1.0.5"), m), UpdateDecision::DowngradeBlocked);
}

void OrionUpdaterTests::evaluateFlagsMandatoryUpdate()
{
    UpdateManifest m = sampleManifest();
    m.mandatory = true;
    QCOMPARE(evaluateUpdate(QStringLiteral("1.0.0"), m), UpdateDecision::MandatoryUpdate);
}

void OrionUpdaterTests::evaluateRefusesDowngradeUnlessRollbackAllowed()
{
    UpdateManifest m = sampleManifest();
    m.version = QStringLiteral("1.0.2");
    QCOMPARE(evaluateUpdate(QStringLiteral("1.0.4"), m), UpdateDecision::DowngradeBlocked);
    m.allowRollback = true;
    QCOMPARE(evaluateUpdate(QStringLiteral("1.0.4"), m), UpdateDecision::UpdateAvailable);
}

void OrionUpdaterTests::evaluateBlocksClientBelowMinimum()
{
    UpdateManifest m = sampleManifest();
    m.minVersion = QStringLiteral("1.0.3");
    QCOMPARE(evaluateUpdate(QStringLiteral("1.0.1"), m), UpdateDecision::ClientBlocked);
    // an invalid manifest is its own decision
    UpdateManifest bad;
    QCOMPARE(evaluateUpdate(QStringLiteral("1.0.1"), bad), UpdateDecision::InvalidManifest);
}

void OrionUpdaterTests::parseReadsChannelField()
{
    const QByteArray payload = R"({
        "version": "1.0.4",
        "channel": "Beta"
    })";
    const UpdateManifest m = parseUpdateManifest(payload);
    QVERIFY(m.ok);
    QCOMPARE(m.channel, QStringLiteral("beta")); // normalized lowercase

    // "ring" variant accepted; absent -> empty
    QCOMPARE(parseUpdateManifest(R"({"version":"1.0.4","ring":"stable"})").channel,
             QStringLiteral("stable"));
    QVERIFY(parseUpdateManifest(R"({"version":"1.0.4"})").channel.isEmpty());
}

void OrionUpdaterTests::channelDoesNotAffectSignature()
{
    // channel is display/routing only — it must NOT enter the canonical signing
    // string, so a signed manifest verifies identically with or without it.
    const QByteArray seed = testSeed();
    const QByteArray publicKey = ed25519DerivePublicKey(seed);

    UpdateManifest m = sampleManifest();
    signManifestEd25519(m, seed);
    QVERIFY(verifyManifestSignature(m, publicKey));

    UpdateManifest withChannel = m;
    withChannel.channel = QStringLiteral("beta");
    QCOMPARE(canonicalManifestSigningString(withChannel), canonicalManifestSigningString(m));
    QVERIFY(verifyManifestSignature(withChannel, publicKey));
}

void OrionUpdaterTests::gatePolicyMatrix()
{
    const bool dev = true, prod = false, updater = true, noUpdater = false;

    // Nothing to do -> no gate.
    QCOMPARE(evaluateUpdateGate(UpdateDecision::UpToDate, prod, updater), UpdateGateAction::Proceed);
    QCOMPARE(evaluateUpdateGate(UpdateDecision::DowngradeBlocked, prod, updater), UpdateGateAction::Proceed);
    QCOMPARE(evaluateUpdateGate(UpdateDecision::InvalidManifest, prod, updater), UpdateGateAction::Proceed);

    // Production install with updater present: optional offers, mandatory/blocked force.
    QCOMPARE(evaluateUpdateGate(UpdateDecision::UpdateAvailable, prod, updater), UpdateGateAction::OfferUpdate);
    QCOMPARE(evaluateUpdateGate(UpdateDecision::MandatoryUpdate, prod, updater), UpdateGateAction::ForceUpdate);
    QCOMPARE(evaluateUpdateGate(UpdateDecision::ClientBlocked, prod, updater), UpdateGateAction::ForceUpdate);

    // Dev guard: a build-tree launch is never forced and never auto-applies.
    QCOMPARE(evaluateUpdateGate(UpdateDecision::UpdateAvailable, dev, updater), UpdateGateAction::OfferUpdate);
    QCOMPARE(evaluateUpdateGate(UpdateDecision::MandatoryUpdate, dev, updater), UpdateGateAction::OfferUpdate);
    QCOMPARE(evaluateUpdateGate(UpdateDecision::ClientBlocked, dev, noUpdater), UpdateGateAction::OfferUpdate);

    // Missing updater: optional updates degrade to the pill; only a server hard
    // block still gates (so the user learns the install is broken).
    QCOMPARE(evaluateUpdateGate(UpdateDecision::UpdateAvailable, prod, noUpdater), UpdateGateAction::Proceed);
    QCOMPARE(evaluateUpdateGate(UpdateDecision::MandatoryUpdate, prod, noUpdater), UpdateGateAction::Proceed);
    QCOMPARE(evaluateUpdateGate(UpdateDecision::ClientBlocked, prod, noUpdater), UpdateGateAction::ForceUpdate);
}

void OrionUpdaterTests::redactionStripsSecretsKeepsTelemetry()
{
    const QString input = QStringLiteral(
        "license_key=ORION-AB12-CD34-EF56 user=someone@example.com\n"
        "\"access_token\": \"sk_live_abcdef123456\"\n"
        "authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dQw4w9WgXcQabc\n"
        "machine_id=WIN-77f3a9d2c4\n"
        "NVDEV-LOCAL-KEY-0001 activated\n"
        "rtt_ms=37.5 jitter_ms=2.1 frame_age_ms=18\n");
    const QString out = redactDiagnosticsText(input);

    // Secrets gone…
    QVERIFY(!out.contains(QStringLiteral("ORION-AB12-CD34-EF56")));
    QVERIFY(!out.contains(QStringLiteral("someone@example.com")));
    QVERIFY(!out.contains(QStringLiteral("sk_live_abcdef123456")));
    QVERIFY(!out.contains(QStringLiteral("eyJhbGciOiJIUzI1NiJ9")));
    QVERIFY(!out.contains(QStringLiteral("WIN-77f3a9d2c4")));
    QVERIFY(!out.contains(QStringLiteral("NVDEV-LOCAL-KEY-0001")));
    // …support correlation suffix kept for an unlabelled key mention (a
    // labelled `license_key=` value is redacted entirely — defense in depth)…
    QVERIFY(out.contains(QStringLiteral("NVDEV-...-0001")));
    // …and telemetry untouched.
    QVERIFY(out.contains(QStringLiteral("rtt_ms=37.5 jitter_ms=2.1 frame_age_ms=18")));
}

void OrionUpdaterTests::safeJoinAcceptsDescendants()
{
    const QString root = QDir::cleanPath(QStringLiteral("C:/orion/install"));
    bool ok = false;
    const QString a = safeJoinWithinRoot(root, QStringLiteral("OrionNative.exe"), &ok);
    QVERIFY(ok);
    QVERIFY(a.endsWith(QStringLiteral("/OrionNative.exe")));

    ok = false;
    const QString b = safeJoinWithinRoot(root, QStringLiteral("qml/sub/Main.qml"), &ok);
    QVERIFY(ok);
    QVERIFY(b.contains(QStringLiteral("/qml/sub/Main.qml")));

    // backslash separators normalize and stay inside root
    ok = false;
    const QString c = safeJoinWithinRoot(root, QStringLiteral("dir\\file.dll"), &ok);
    QVERIFY(ok);
    QVERIFY(c.endsWith(QStringLiteral("/dir/file.dll")));
}

void OrionUpdaterTests::safeJoinRejectsTraversalAndAbsolute()
{
    const QString root = QDir::cleanPath(QStringLiteral("C:/orion/install"));
    bool ok = true;
    safeJoinWithinRoot(root, QStringLiteral("../evil.txt"), &ok);
    QVERIFY(!ok);
    ok = true;
    safeJoinWithinRoot(root, QStringLiteral("..\\..\\windows\\system32\\evil.dll"), &ok);
    QVERIFY(!ok);
    ok = true;
    safeJoinWithinRoot(root, QStringLiteral("sub/../../escape.txt"), &ok);
    QVERIFY(!ok);
    ok = true;
    safeJoinWithinRoot(root, QStringLiteral("/etc/passwd"), &ok);
    QVERIFY(!ok);
    ok = true;
    safeJoinWithinRoot(root, QStringLiteral("C:/Windows/system32/x.dll"), &ok);
    QVERIFY(!ok);
    ok = true;
    safeJoinWithinRoot(root, QStringLiteral("C:evil.dll"), &ok); // drive-relative
    QVERIFY(!ok);
    ok = true;
    safeJoinWithinRoot(root, QString(), &ok);
    QVERIFY(!ok);
}

void OrionUpdaterTests::extractRejectsPathTraversalZip()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    // QZipReader only strips a *leading* "../" or "/", but passes interior
    // traversal and absolute drive paths through verbatim — so extractZipSafely's
    // own validation is what actually stops these.

    // Interior traversal that escapes the staging dir into its parent.
    {
        const QString zipPath = tmp.filePath(QStringLiteral("evil1.zip"));
        QVERIFY(writeStoredZip(zipPath, QStringLiteral("a/../../escape.txt"), QByteArrayLiteral("pwned")));
        const QString dest = tmp.filePath(QStringLiteral("stage"));
        const ExtractResult result = extractZipSafely(zipPath, dest);
        QVERIFY(!result.ok);
        QCOMPARE(result.rejectedEntry, QStringLiteral("a/../../escape.txt"));
        QVERIFY(!QFile::exists(tmp.filePath(QStringLiteral("escape.txt"))));
    }
    // Absolute drive path must be rejected before anything is written.
    {
        const QString zipPath = tmp.filePath(QStringLiteral("evil2.zip"));
        const QString abs = QDir::cleanPath(tmp.path() + QStringLiteral("/outside/orion_escape.txt"));
        QVERIFY(writeStoredZip(zipPath, abs, QByteArrayLiteral("pwned")));
        const QString dest = tmp.filePath(QStringLiteral("stage2"));
        const ExtractResult result = extractZipSafely(zipPath, dest);
        QVERIFY(!result.ok);
        QVERIFY(!QFile::exists(abs));
    }
}

void OrionUpdaterTests::extractRejectsSymlinkZip()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString zipPath = tmp.filePath(QStringLiteral("link.zip"));
    {
        QZipWriter writer(zipPath);
        writer.addFile(QStringLiteral("readme.txt"), QByteArrayLiteral("hi"));
        writer.addSymLink(QStringLiteral("link"), QStringLiteral("C:/Windows/system32"));
        writer.close();
    }
    const QString dest = tmp.filePath(QStringLiteral("stage"));
    const ExtractResult result = extractZipSafely(zipPath, dest);
    QVERIFY(!result.ok);
}

void OrionUpdaterTests::extractsCleanZip()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString zipPath = tmp.filePath(QStringLiteral("good.zip"));
    {
        QZipWriter writer(zipPath);
        writer.addFile(QStringLiteral("OrionNative.exe"), QByteArrayLiteral("MZbinary"));
        writer.addDirectory(QStringLiteral("qml"));
        writer.addFile(QStringLiteral("qml/Main.qml"), QByteArrayLiteral("import QtQuick"));
        writer.close();
    }
    const QString dest = tmp.filePath(QStringLiteral("stage"));
    const ExtractResult result = extractZipSafely(zipPath, dest);
    QVERIFY2(result.ok, qPrintable(result.error));
    QVERIFY(QFile::exists(dest + QStringLiteral("/OrionNative.exe")));
    QVERIFY(QFile::exists(dest + QStringLiteral("/qml/Main.qml")));
    QFile f(dest + QStringLiteral("/qml/Main.qml"));
    QVERIFY(f.open(QIODevice::ReadOnly));
    QCOMPARE(f.readAll(), QByteArrayLiteral("import QtQuick"));
}

void OrionUpdaterTests::backupRestoreRoundTrip()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/qml")));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/qml/Main.qml"), QByteArrayLiteral("qml-v1")));

    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));

    // Corrupt the install.
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("CORRUPT")));
    QFile::remove(install + QStringLiteral("/qml/Main.qml"));

    QVERIFY2(restoreTree(backup, install, &err), qPrintable(err));
    QFile exe(install + QStringLiteral("/OrionNative.exe"));
    QVERIFY(exe.open(QIODevice::ReadOnly));
    QCOMPARE(exe.readAll(), QByteArrayLiteral("v1"));
    QVERIFY(QFile::exists(install + QStringLiteral("/qml/Main.qml")));
}

void OrionUpdaterTests::applyTreeOverwritesAndAdds()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    QVERIFY(QDir().mkpath(install));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/keep.txt"), QByteArrayLiteral("keep")));

    const QString staged = tmp.filePath(QStringLiteral("staged"));
    QVERIFY(QDir().mkpath(staged + QStringLiteral("/new")));
    QVERIFY(writeTextFile(staged + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v2")));
    QVERIFY(writeTextFile(staged + QStringLiteral("/new/extra.dll"), QByteArrayLiteral("added")));

    QStringList applied;
    QString err;
    QVERIFY2(applyTree(staged, install, &applied, &err), qPrintable(err));

    QFile exe(install + QStringLiteral("/OrionNative.exe"));
    QVERIFY(exe.open(QIODevice::ReadOnly));
    QCOMPARE(exe.readAll(), QByteArrayLiteral("v2"));            // overwritten
    QVERIFY(QFile::exists(install + QStringLiteral("/new/extra.dll"))); // added
    QVERIFY(QFile::exists(install + QStringLiteral("/keep.txt")));      // untouched
    QVERIFY(applied.contains(QStringLiteral("OrionNative.exe")));
}

void OrionUpdaterTests::failedApplyRollsBackToBackup()
{
    // Models the updater's rollback path: snapshot install -> apply staged ->
    // declare failure -> restore from backup -> install is byte-identical to start.
    // [Codex F4] The old version of this test never failed the apply; the real
    // mid-apply failure is now restoreTreeIsExactAfterAPartialApply(). This one keeps
    // the "post-apply failure detected" shape and now also asserts that a file the
    // broken build ADDED is gone after the restore.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/qml")));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/qml/Main.qml"), QByteArrayLiteral("qml-v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/orion_updater.log"), QByteArrayLiteral("log")));

    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));

    // Apply a partial/broken new build that also adds a brand-new DLL.
    const QString staged = tmp.filePath(QStringLiteral("staged"));
    QVERIFY(QDir().mkpath(staged + QStringLiteral("/newdir")));
    QVERIFY(writeTextFile(staged + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v2-broken")));
    QVERIFY(writeTextFile(staged + QStringLiteral("/Extra.dll"), QByteArrayLiteral("new")));
    QVERIFY(writeTextFile(staged + QStringLiteral("/newdir/thing.bin"), QByteArrayLiteral("new")));
    QVERIFY2(applyTree(staged, install, nullptr, &err), qPrintable(err));
    QVERIFY(QFile::exists(install + QStringLiteral("/Extra.dll")));

    // Failure detected post-apply -> roll back. The log written DURING the update survives
    // WITH its contents (the backup must not snapshot it, the restore must not overwrite it).
    QVERIFY(writeTextFile(install + QStringLiteral("/orion_updater.log"), QByteArrayLiteral("log+apply")));
    QVERIFY(!QFile::exists(backup + QStringLiteral("/orion_updater.log")));
    QVERIFY2(restoreTree(backup, install, &err), qPrintable(err));

    QFile exe(install + QStringLiteral("/OrionNative.exe"));
    QVERIFY(exe.open(QIODevice::ReadOnly));
    QCOMPARE(exe.readAll(), QByteArrayLiteral("v1"));
    QFile qml(install + QStringLiteral("/qml/Main.qml"));
    QVERIFY(qml.open(QIODevice::ReadOnly));
    QCOMPARE(qml.readAll(), QByteArrayLiteral("qml-v1"));
    QVERIFY(!QFile::exists(install + QStringLiteral("/Extra.dll")));
    QVERIFY(!QFileInfo::exists(install + QStringLiteral("/newdir")));
    QFile liveLog(install + QStringLiteral("/orion_updater.log"));
    QVERIFY(liveLog.open(QIODevice::ReadOnly));
    QCOMPARE(liveLog.readAll(), QByteArrayLiteral("log+apply"));
}

// ---------------------------------------------------------------- 2026-09-21
namespace {

QByteArray hexKeyDecoder(const QString& encoded)
{
    return QByteArray::fromHex(encoded.toLatin1());
}

QString hex32(char fill)
{
    return QString::fromLatin1(QByteArray(32, fill).toHex());
}

bool writePubkeysFile(const QString& path, const QString& id, const QString& hexKey)
{
    QJsonObject keys;
    keys.insert(id, hexKey);
    QJsonObject root;
    root.insert(QStringLiteral("keys"), keys);
    return writeTextFile(path, QJsonDocument(root).toJson(QJsonDocument::Compact));
}

// NTFS junction (mklink /J needs no privilege). On Windows this MUST succeed -- the
// security cases below are part of the release gate and may not silently skip
// ([Codex F6]); only a non-Windows host skips them.
#if defined(Q_OS_WIN)
// Win32 junction creation (no cmd.exe: mklink cannot address paths beyond MAX_PATH, and
// the long-path case is exactly one of the security cases). Junctions need no privilege.
struct JunctionReparseData {
    ULONG ReparseTag;
    USHORT ReparseDataLength;
    USHORT Reserved;
    USHORT SubstituteNameOffset;
    USHORT SubstituteNameLength;
    USHORT PrintNameOffset;
    USHORT PrintNameLength;
    WCHAR PathBuffer[1];
};

QString extendedPath(const QString& path)
{
    QString native = QDir::toNativeSeparators(QDir::cleanPath(QFileInfo(path).absoluteFilePath()));
    if (!native.startsWith(QStringLiteral("\\\\?\\"))) {
        native.prepend(QStringLiteral("\\\\?\\"));
    }
    return native;
}
#endif

bool makeJunction(const QString& link, const QString& target)
{
#if defined(Q_OS_WIN)
    const QString linkExt = extendedPath(link);
    if (!CreateDirectoryW(reinterpret_cast<LPCWSTR>(linkExt.utf16()), nullptr)) {
        return false;
    }
    HANDLE h = CreateFileW(reinterpret_cast<LPCWSTR>(linkExt.utf16()), GENERIC_WRITE, 0, nullptr, OPEN_EXISTING,
                           FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        return false;
    }
    // Substitute name is the NT form of the target; the print name is the Win32 form.
    const QString targetNative = QDir::toNativeSeparators(QDir::cleanPath(QFileInfo(target).absoluteFilePath()));
    const QString substitute = QStringLiteral("\\??\\") + targetNative;
    const int subBytes = substitute.size() * 2;
    const int printBytes = targetNative.size() * 2;
    const int pathBufferBytes = subBytes + 2 + printBytes + 2;
    QByteArray buf(static_cast<int>(offsetof(JunctionReparseData, PathBuffer)) + pathBufferBytes, '\0');
    auto* rd = reinterpret_cast<JunctionReparseData*>(buf.data());
    rd->ReparseTag = IO_REPARSE_TAG_MOUNT_POINT;
    rd->SubstituteNameOffset = 0;
    rd->SubstituteNameLength = static_cast<USHORT>(subBytes);
    rd->PrintNameOffset = static_cast<USHORT>(subBytes + 2);
    rd->PrintNameLength = static_cast<USHORT>(printBytes);
    memcpy(rd->PathBuffer, substitute.utf16(), subBytes);
    memcpy(reinterpret_cast<char*>(rd->PathBuffer) + subBytes + 2, targetNative.utf16(), printBytes);
    rd->ReparseDataLength = static_cast<USHORT>(pathBufferBytes + 8);   // the four USHORT fields
    DWORD returned = 0;
    const BOOL ok = DeviceIoControl(h, FSCTL_SET_REPARSE_POINT, buf.data(), static_cast<DWORD>(buf.size()),
                                    nullptr, 0, &returned, nullptr);
    CloseHandle(h);
    return ok && isReparsePointPath(link);
#else
    Q_UNUSED(link); Q_UNUSED(target);
    return false;
#endif
}

bool makeHardlink(const QString& link, const QString& target)
{
#if defined(Q_OS_WIN)
    const int rc = QProcess::execute(QStringLiteral("cmd.exe"),
                                     {QStringLiteral("/c"), QStringLiteral("mklink"), QStringLiteral("/H"),
                                      QDir::toNativeSeparators(link), QDir::toNativeSeparators(target)});
    return rc == 0 && QFileInfo::exists(link);
#else
    Q_UNUSED(link); Q_UNUSED(target);
    return false;
#endif
}

#if defined(Q_OS_WIN)
#define ORION_REQUIRE_LINK(expr) QVERIFY2((expr), "could not create the NTFS link this security case needs")
#else
#define ORION_REQUIRE_LINK(expr) do { if (!(expr)) QSKIP("NTFS links are Windows-only"); } while (0)
#endif

QList<EmbeddedKey> oneEmbedded(const QString& id, const QByteArray& key)
{
    return {EmbeddedKey{id, key}};
}

// Like writeStoredZip, but the central-directory record declares `declaredSize` bytes
// while the entry really carries data.size(): what a truncated download looks like.
bool writeStoredZipDeclaring(const QString& path, const QString& entryName, const QByteArray& data,
                             quint32 declaredSize)
{
    const QByteArray name = entryName.toUtf8();
    const quint32 crc = crc32Of(data);
    const quint32 dataLen = static_cast<quint32>(data.size());
    const quint16 nameLen = static_cast<quint16>(name.size());
    QByteArray zip;
    putLE32(zip, 0x04034b50);
    putLE16(zip, 20); putLE16(zip, 0); putLE16(zip, 0); putLE16(zip, 0); putLE16(zip, 0);
    putLE32(zip, crc); putLE32(zip, dataLen); putLE32(zip, dataLen);
    putLE16(zip, nameLen); putLE16(zip, 0);
    zip.append(name);
    zip.append(data);
    const quint32 cdOffset = static_cast<quint32>(zip.size());
    QByteArray cd;
    putLE32(cd, 0x02014b50);
    putLE16(cd, 20); putLE16(cd, 20); putLE16(cd, 0); putLE16(cd, 0); putLE16(cd, 0); putLE16(cd, 0);
    putLE32(cd, crc); putLE32(cd, dataLen); putLE32(cd, declaredSize);
    putLE16(cd, nameLen); putLE16(cd, 0); putLE16(cd, 0); putLE16(cd, 0); putLE16(cd, 0);
    putLE32(cd, 0); putLE32(cd, 0);
    cd.append(name);
    zip.append(cd);
    putLE32(zip, 0x06054b50);
    putLE16(zip, 0); putLE16(zip, 0); putLE16(zip, 1); putLE16(zip, 1);
    putLE32(zip, static_cast<quint32>(cd.size())); putLE32(zip, cdOffset);
    putLE16(zip, 0);
    QFile f(path);
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return false;
    }
    return f.write(zip) == zip.size();
}

// File symlink (needs SeCreateSymbolicLinkPrivilege or Developer Mode). Returns false when
// the privilege is missing; the one test that needs it skips with that reason -- there is
// no unprivileged way to create a file symlink on Windows.
bool makeFileSymlink(const QString& link, const QString& target)
{
#if defined(Q_OS_WIN)
    const QString l = QDir::toNativeSeparators(link);
    const QString tg = QDir::toNativeSeparators(target);
    const DWORD flags = 0x2; // SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE (Developer Mode)
    if (CreateSymbolicLinkW(reinterpret_cast<LPCWSTR>(l.utf16()), reinterpret_cast<LPCWSTR>(tg.utf16()), flags)) {
        return isReparsePointPath(link);
    }
    if (CreateSymbolicLinkW(reinterpret_cast<LPCWSTR>(l.utf16()), reinterpret_cast<LPCWSTR>(tg.utf16()), 0)) {
        return isReparsePointPath(link);
    }
    return false;
#else
    Q_UNUSED(link); Q_UNUSED(target);
    return false;
#endif
}

} // namespace

void OrionUpdaterTests::trustRootProductionTrustsOnlyEmbeddedKey()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QByteArray embedded(32, 'E');
    const QString rogueId = QStringLiteral("attacker-key-1");
    // Every external source offers the attacker's id: CLI file, env file, install dir, app dir.
    const QString cliFile = tmp.filePath(QStringLiteral("cli.json"));
    const QString envFile = tmp.filePath(QStringLiteral("env.json"));
    const QString installDir = tmp.filePath(QStringLiteral("install"));
    const QString appDir = tmp.filePath(QStringLiteral("app"));
    QVERIFY(QDir().mkpath(installDir) && QDir().mkpath(appDir));
    QVERIFY(writePubkeysFile(cliFile, rogueId, hex32('A')));
    QVERIFY(writePubkeysFile(envFile, rogueId, hex32('B')));
    QVERIFY(writePubkeysFile(installDir + QStringLiteral("/update_pubkeys.json"), rogueId, hex32('C')));
    QVERIFY(writePubkeysFile(appDir + QStringLiteral("/update_pubkeys.json"), rogueId, hex32('D')));
    TrustRootSources sources;
    sources.cliPubkeysFile = cliFile;
    sources.envPubkeysFile = envFile;
    sources.installDir = installDir;
    sources.applicationDir = appDir;

    // The embedded id resolves to the embedded key and nothing else is consulted.
    const TrustRootDecision ok = resolveTrustRoot(QStringLiteral("orion-ed25519-v1"),
                                                  oneEmbedded(QStringLiteral("orion-ed25519-v1"), embedded),
                                                  sources, /*production*/ true, hexKeyDecoder);
    QCOMPARE(ok.publicKey, embedded);
    QCOMPARE(ok.source, QStringLiteral("embedded"));

    // Any other id fails CLOSED in production, and every offered override is named.
    const TrustRootDecision rogue = resolveTrustRoot(rogueId, oneEmbedded(QStringLiteral("orion-ed25519-v1"), embedded),
                                                     sources, /*production*/ true, hexKeyDecoder);
    QVERIFY(rogue.publicKey.isEmpty());
    QVERIFY(rogue.source.isEmpty());
    QCOMPARE(rogue.ignored.size(), 4);
    QVERIFY(rogue.ignored.at(0).startsWith(QStringLiteral("cli:")));
    QVERIFY(rogue.ignored.at(1).startsWith(QStringLiteral("env:")));
    QVERIFY(rogue.ignored.at(2).startsWith(QStringLiteral("install_dir:")));
    QVERIFY(rogue.ignored.at(3).startsWith(QStringLiteral("app_dir:")));

    // Even the embedded id cannot be served from a file when the binary has no key.
    const TrustRootDecision noEmbedded = resolveTrustRoot(QStringLiteral("orion-ed25519-v1"), {},
                                                          sources, /*production*/ true, hexKeyDecoder);
    QVERIFY(noEmbedded.publicKey.isEmpty());
}

void OrionUpdaterTests::trustRootDevResolvesExternalSourcesInOrder()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString id = QStringLiteral("orion-rotated-2");
    const QString cliFile = tmp.filePath(QStringLiteral("cli.json"));
    const QString envFile = tmp.filePath(QStringLiteral("env.json"));
    const QString installDir = tmp.filePath(QStringLiteral("install"));
    QVERIFY(QDir().mkpath(installDir));
    QVERIFY(writePubkeysFile(envFile, id, hex32('B')));
    QVERIFY(writePubkeysFile(installDir + QStringLiteral("/update_pubkeys.json"), id, hex32('C')));
    TrustRootSources sources;
    sources.cliPubkeysFile = cliFile;          // does not exist -> skipped
    sources.envPubkeysFile = envFile;
    sources.installDir = installDir;

    const TrustRootDecision dev = resolveTrustRoot(id, oneEmbedded(QStringLiteral("orion-ed25519-v1"), QByteArray(32, 'E')),
                                                   sources, /*production*/ false, hexKeyDecoder);
    QCOMPARE(dev.publicKey, QByteArray(32, 'B'));   // env wins over install_dir; cli was absent
    QCOMPARE(dev.source, QStringLiteral("env"));
    QVERIFY(dev.ignored.isEmpty());

    // A wrong-length key in a file is not a key.
    QVERIFY(writePubkeysFile(cliFile, id, QStringLiteral("abcd")));
    const TrustRootDecision shortKey = resolveTrustRoot(id, {}, sources, /*production*/ false, hexKeyDecoder);
    QCOMPARE(shortKey.source, QStringLiteral("env"));

    // Unknown id everywhere -> fail closed in dev too.
    const TrustRootDecision unknown = resolveTrustRoot(QStringLiteral("nobody"), {}, sources,
                                                       /*production*/ false, hexKeyDecoder);
    QVERIFY(unknown.publicKey.isEmpty());
}

void OrionUpdaterTests::localManifestRefusedInProduction()
{
    QVERIFY(!localManifestAllowed(true));
    QVERIFY(localManifestAllowed(false));
}

void OrionUpdaterTests::reparsePointDetectedUnderRoot()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString root = tmp.filePath(QStringLiteral("root"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    QVERIFY(QDir().mkpath(root + QStringLiteral("/plain/sub")) && QDir().mkpath(outside));
    // Ordinary descendants, existing or not, are fine; the root itself is fine.
    QVERIFY(!pathCrossesReparsePoint(root, root));
    QVERIFY(!pathCrossesReparsePoint(root, root + QStringLiteral("/plain/sub/new.txt")));
    QVERIFY(!pathCrossesReparsePoint(root, root + QStringLiteral("/not/yet/there.txt")));
    // Anything not under the root fails closed.
    QVERIFY(pathCrossesReparsePoint(root, outside + QStringLiteral("/x.txt")));
    ORION_REQUIRE_LINK(makeJunction(root + QStringLiteral("/junc"), outside));
    QVERIFY(isReparsePointPath(root + QStringLiteral("/junc")));
    QVERIFY(!isReparsePointPath(root + QStringLiteral("/plain")));
    QVERIFY(!isReparsePointPath(root + QStringLiteral("/missing")));
    QVERIFY(pathCrossesReparsePoint(root, root + QStringLiteral("/junc")));
    QVERIFY(pathCrossesReparsePoint(root, root + QStringLiteral("/junc/evil.dll")));
    QVERIFY(pathCrossesReparsePoint(root, root + QStringLiteral("/junc/deeper/evil.dll")));
    QVERIFY(!pathCrossesReparsePoint(root, root + QStringLiteral("/plain/ok.dll")));
}

void OrionUpdaterTests::applyTreeRefusesJunctionInInstallDir()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    QVERIFY(QDir().mkpath(install) && QDir().mkpath(outside) && QDir().mkpath(stage + QStringLiteral("/plugins")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/plugins/evil.dll"), QByteArrayLiteral("pwned")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/good.txt"), QByteArrayLiteral("fine")));
    ORION_REQUIRE_LINK(makeJunction(install + QStringLiteral("/plugins"), outside));
    QString error;
    QStringList applied;
    QVERIFY(!applyTree(stage, install, &applied, &error));
    QVERIFY2(error.contains(QStringLiteral("reparse")), qPrintable(error));
    // Nothing went through the junction.
    QVERIFY(!QFile::exists(outside + QStringLiteral("/evil.dll")));
}

void OrionUpdaterTests::backupTreeRefusesJunctionInInstallDir()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QVERIFY(QDir().mkpath(install) && QDir().mkpath(outside));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("exe")));
    QVERIFY(writeTextFile(outside + QStringLiteral("/secret.txt"), QByteArrayLiteral("not-yours")));
    ORION_REQUIRE_LINK(makeJunction(install + QStringLiteral("/data"), outside));
    QString error;
    // The source tree contains a reparse point: a backup must not follow it.
    QVERIFY(!backupTree(install, backup, &error));
    QVERIFY2(error.contains(QStringLiteral("reparse")), qPrintable(error));
    QVERIFY(!QFile::exists(backup + QStringLiteral("/data/secret.txt")));
}

void OrionUpdaterTests::trustRootProductionAcceptsEveryEmbeddedKeyAndNothingElse()
{
    // [Codex F5] During a planned rotation both ids are embedded; a retired id is not.
    const QList<EmbeddedKey> embedded = {
        EmbeddedKey{QStringLiteral("orion-ed25519-v1"), QByteArray(32, 'A')},
        EmbeddedKey{QStringLiteral("orion-ed25519-v2"), QByteArray(32, 'B')},
    };
    TrustRootSources sources;
    const TrustRootDecision v1 = resolveTrustRoot(QStringLiteral("orion-ed25519-v1"), embedded, sources, true, hexKeyDecoder);
    const TrustRootDecision v2 = resolveTrustRoot(QStringLiteral("orion-ed25519-v2"), embedded, sources, true, hexKeyDecoder);
    QCOMPARE(v1.publicKey, QByteArray(32, 'A'));
    QCOMPARE(v2.publicKey, QByteArray(32, 'B'));
    QCOMPARE(v1.source, QStringLiteral("embedded"));
    const TrustRootDecision retired = resolveTrustRoot(QStringLiteral("orion-ed25519-v0"), embedded, sources, true, hexKeyDecoder);
    QVERIFY(retired.publicKey.isEmpty());
    // A malformed embedded entry (wrong length) never resolves, even by id.
    const QList<EmbeddedKey> broken = {EmbeddedKey{QStringLiteral("orion-ed25519-v3"), QByteArray(31, 'C')}};
    QVERIFY(resolveTrustRoot(QStringLiteral("orion-ed25519-v3"), broken, sources, true, hexKeyDecoder).publicKey.isEmpty());
}

void OrionUpdaterTests::installRootBindingRefusesReparseRootAndForeignDirInProduction()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString appDir = tmp.filePath(QStringLiteral("app"));
    const QString other = tmp.filePath(QStringLiteral("other"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    QVERIFY(QDir().mkpath(appDir) && QDir().mkpath(other) && QDir().mkpath(outside));
    QString error;
    // Production: only the updater's own directory.
    QCOMPARE(bindInstallRoot(QString(), appDir, true, &error), QDir::cleanPath(appDir));
    QCOMPARE(bindInstallRoot(appDir, appDir, true, &error), QDir::cleanPath(appDir));
    QVERIFY(bindInstallRoot(other, appDir, true, &error).isEmpty());
    QVERIFY2(error.contains(QStringLiteral("updater's own directory")), qPrintable(error));
    // Development: an explicit --install-dir is allowed (tests use it)...
    QCOMPARE(bindInstallRoot(other, appDir, false, &error), QDir::cleanPath(other));
    // ...but a reparse-point root never is, in either profile.
    const QString junc = tmp.filePath(QStringLiteral("junc"));
    ORION_REQUIRE_LINK(makeJunction(junc, outside));
    QVERIFY(bindInstallRoot(junc, appDir, false, &error).isEmpty());
    QVERIFY2(error.contains(QStringLiteral("reparse")), qPrintable(error));
    QVERIFY(bindInstallRoot(junc, junc, true, &error).isEmpty());
    // A missing directory is refused too.
    QVERIFY(bindInstallRoot(tmp.filePath(QStringLiteral("nope")), appDir, false, &error).isEmpty());
    // [Codex r6 F3] A UNC root is refused in every profile, with its own reason, before any
    // filesystem access (the share need not exist).
    QVERIFY(bindInstallRoot(QStringLiteral("//server/share/Venice"), appDir, false, &error).isEmpty());
    QVERIFY2(error.contains(QStringLiteral("network (UNC)")), qPrintable(error));
    QVERIFY(bindInstallRoot(QStringLiteral("//server/share/Venice"), QStringLiteral("//server/share/Venice"), true, &error).isEmpty());
    QVERIFY2(error.contains(QStringLiteral("network (UNC)")), qPrintable(error));
}

void OrionUpdaterTests::rootJunctionIsRefusedForEveryWrite()
{
    // [Codex F2] The root itself is not exempt.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    QVERIFY(QDir().mkpath(outside) && QDir().mkpath(stage));
    QVERIFY(writeTextFile(stage + QStringLiteral("/x.dll"), QByteArrayLiteral("x")));
    const QString root = tmp.filePath(QStringLiteral("root"));
    ORION_REQUIRE_LINK(makeJunction(root, outside));
    QVERIFY(pathCrossesReparsePoint(root, root));
    QVERIFY(pathCrossesReparsePoint(root, root + QStringLiteral("/x.dll")));
    QString error;
    QVERIFY(!applyTree(stage, root, nullptr, &error));
    QVERIFY(!QFile::exists(outside + QStringLiteral("/x.dll")));
    QVERIFY(!backupTree(root, tmp.filePath(QStringLiteral("backup")), &error));
    // Extraction into a junction staging root is refused as well.
    const QString zipPath = tmp.filePath(QStringLiteral("ok.zip"));
    QVERIFY(writeStoredZip(zipPath, QStringLiteral("a.txt"), QByteArrayLiteral("a")));
    const QString stageJunc = tmp.filePath(QStringLiteral("stagejunc"));
    // extractZipSafely wipes and recreates its destination, so a pre-made junction
    // cannot survive to be tested there; the guard is exercised through the root check.
    QVERIFY(extractZipSafely(zipPath, stageJunc).ok);
}

void OrionUpdaterTests::applyTreeDoesNotCreateDirectoriesThroughAJunction()
{
    // [Codex F3] mkpath used to run before the reparse check, so <install>/plugins/sub
    // was created THROUGH the junction (outside/sub appeared) before the refusal.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    QVERIFY(QDir().mkpath(install) && QDir().mkpath(outside) && QDir().mkpath(stage + QStringLiteral("/plugins/sub")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/plugins/sub/evil.dll"), QByteArrayLiteral("pwned")));
    ORION_REQUIRE_LINK(makeJunction(install + QStringLiteral("/plugins"), outside));
    QString error;
    QVERIFY(!applyTree(stage, install, nullptr, &error));
    QVERIFY(!QFileInfo::exists(outside + QStringLiteral("/sub")));
    QVERIFY(!QFile::exists(outside + QStringLiteral("/sub/evil.dll")));
}

void OrionUpdaterTests::restoreTreeIsExactAfterAPartialApply()
{
    // [Codex F4] A real mid-apply failure: "Extra.dll" sorts before "plugins" and is
    // written, then the junction refuses the rest. After restore the tree is exactly
    // the backup: Extra.dll gone, originals byte-identical.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/qml")) && QDir().mkpath(outside));
    QVERIFY(QDir().mkpath(stage + QStringLiteral("/plugins")));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/qml/Main.qml"), QByteArrayLiteral("qml-v1")));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));
    // The junction is planted AFTER the backup (the backup itself refuses reparse points).
    ORION_REQUIRE_LINK(makeJunction(install + QStringLiteral("/plugins"), outside));
    QVERIFY(writeTextFile(stage + QStringLiteral("/Extra.dll"), QByteArrayLiteral("new")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v2")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/plugins/evil.dll"), QByteArrayLiteral("pwned")));

    QStringList applied;
    QVERIFY(!applyTree(stage, install, &applied, &err));        // fails at the junction
    QVERIFY2(err.contains(QStringLiteral("reparse")), qPrintable(err));
    QVERIFY(QFile::exists(install + QStringLiteral("/Extra.dll")));   // the partial write happened
    // Drop the junction as a failed real update would leave the tree, then roll back.
    QVERIFY(QDir().rmdir(install + QStringLiteral("/plugins")));
    QVERIFY2(restoreTree(backup, install, &err), qPrintable(err));

    QVERIFY(!QFile::exists(install + QStringLiteral("/Extra.dll")));
    QFile exe(install + QStringLiteral("/OrionNative.exe"));
    QVERIFY(exe.open(QIODevice::ReadOnly));
    QCOMPARE(exe.readAll(), QByteArrayLiteral("v1"));
    QFile qml(install + QStringLiteral("/qml/Main.qml"));
    QVERIFY(qml.open(QIODevice::ReadOnly));
    QCOMPARE(qml.readAll(), QByteArrayLiteral("qml-v1"));
    // Exact: the set of files equals the backup's.
    QStringList installFiles, backupFiles;
    for (QDirIterator it(install, QDir::Files, QDirIterator::Subdirectories); it.hasNext();) {
        installFiles << QDir(install).relativeFilePath(it.next());
    }
    for (QDirIterator it(backup, QDir::Files, QDirIterator::Subdirectories); it.hasNext();) {
        backupFiles << QDir(backup).relativeFilePath(it.next());
    }
    installFiles.sort();
    backupFiles.sort();
    QCOMPARE(installFiles, backupFiles);
    QVERIFY(!QFile::exists(outside + QStringLiteral("/evil.dll")));
}

void OrionUpdaterTests::applyTreeOverwritingHardlinkLeavesTheOtherLinkIntact()
{
    // [Codex F3] Hardlink regression: overwriting a name that is a hardlink must not
    // rewrite the other link's bytes (remove-then-copy detaches it).
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    QVERIFY(QDir().mkpath(install) && QDir().mkpath(outside) && QDir().mkpath(stage));
    QVERIFY(writeTextFile(outside + QStringLiteral("/target.dll"), QByteArrayLiteral("original")));
    ORION_REQUIRE_LINK(makeHardlink(install + QStringLiteral("/Orion.dll"), outside + QStringLiteral("/target.dll")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/Orion.dll"), QByteArrayLiteral("updated")));
    QString err;
    QVERIFY2(applyTree(stage, install, nullptr, &err), qPrintable(err));
    QFile a(install + QStringLiteral("/Orion.dll"));
    QVERIFY(a.open(QIODevice::ReadOnly));
    QCOMPARE(a.readAll(), QByteArrayLiteral("updated"));
    QFile b(outside + QStringLiteral("/target.dll"));
    QVERIFY(b.open(QIODevice::ReadOnly));
    QCOMPARE(b.readAll(), QByteArrayLiteral("original"));
}

void OrionUpdaterTests::rollbackPreservesTheLiveUpdaterLog()
{
    // [Codex r2 F4] The failure/rollback entries are appended to the live log while the
    // update is in flight; a restore that copied the backup's older log over it would
    // destroy exactly the evidence an incident needs.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QVERIFY(QDir().mkpath(install));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/orion_updater.log"), QByteArrayLiteral("before\n")));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));
    QVERIFY(!QFile::exists(backup + QStringLiteral("/orion_updater.log")));   // evidence is not payload
    QVERIFY(writeTextFile(install + QStringLiteral("/orion_updater.log"),
                          QByteArrayLiteral("before\nERROR: apply failed\nrolling back\n")));
    QVERIFY2(restoreTree(backup, install, &err), qPrintable(err));
    QFile log(install + QStringLiteral("/orion_updater.log"));
    QVERIFY(log.open(QIODevice::ReadOnly));
    const QByteArray contents = log.readAll();
    QVERIFY2(contents.contains("ERROR: apply failed") && contents.contains("rolling back"), contents.constData());
}

void OrionUpdaterTests::reparseLookupFailsClosedAndHandlesLongPaths()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
#if defined(Q_OS_WIN)
    // A name Win32 cannot look up (invalid characters) is UNSAFE, not "not a reparse point".
    QVERIFY(isReparsePointPath(tmp.filePath(QStringLiteral("bad<name>|.txt"))));
    // Beyond MAX_PATH: a plain directory is plain, a junction is a reparse point, and the
    // walk from the root still sees it.
    QString deep = tmp.path();
    for (int i = 0; i < 12; ++i) {
        deep += QStringLiteral("/segment_%1_abcdefghijklmnopqrstuvwxyz").arg(i);
    }
    QVERIFY(deep.size() > 260);
    QVERIFY(QDir().mkpath(deep));
    QVERIFY(!isReparsePointPath(deep));
    QVERIFY(!pathCrossesReparsePoint(tmp.path(), deep + QStringLiteral("/new.txt")));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    QVERIFY(QDir().mkpath(outside));
    ORION_REQUIRE_LINK(makeJunction(deep + QStringLiteral("/junc"), outside));
    QVERIFY(isReparsePointPath(deep + QStringLiteral("/junc")));
    QVERIFY(pathCrossesReparsePoint(tmp.path(), deep + QStringLiteral("/junc/evil.dll")));
    QVERIFY(!pathCrossesReparsePoint(tmp.path(), deep + QStringLiteral("/plain/evil.dll")));
#else
    QSKIP("Windows attribute lookup semantics");
#endif
}

void OrionUpdaterTests::buildProfileOutputIsConfinedToTemp()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString temp = tmp.filePath(QStringLiteral("temp"));
    const QString elsewhere = tmp.filePath(QStringLiteral("elsewhere"));
    QVERIFY(QDir().mkpath(temp) && QDir().mkpath(elsewhere));
    QString err;
    // A new file directly under temp: allowed.
    QVERIFY(!buildProfileOutputPath(temp + QStringLiteral("/profile.json"), temp, &err).isEmpty());
    // Outside temp: refused.
    QVERIFY(buildProfileOutputPath(elsewhere + QStringLiteral("/profile.json"), temp, &err).isEmpty());
    QVERIFY(buildProfileOutputPath(temp + QStringLiteral("/../elsewhere/profile.json"), temp, &err).isEmpty());
    // An existing file (overwrite primitive): refused.
    QVERIFY(writeTextFile(temp + QStringLiteral("/exists.json"), QByteArrayLiteral("x")));
    QVERIFY(buildProfileOutputPath(temp + QStringLiteral("/exists.json"), temp, &err).isEmpty());
    // Through a junction under temp: refused.
    ORION_REQUIRE_LINK(makeJunction(temp + QStringLiteral("/junc"), elsewhere));
    QVERIFY(buildProfileOutputPath(temp + QStringLiteral("/junc/profile.json"), temp, &err).isEmpty());
    QVERIFY(buildProfileOutputPath(QString(), temp, &err).isEmpty());
}

void OrionUpdaterTests::relaunchNameIsPinnedInProductionAndSanitisedInDev()
{
    // [Codex r3 F7] An elevated updater must never launch a caller-chosen path.
    bool ignored = false;
    QString err;
    QCOMPARE(relaunchExecutableName(QStringLiteral("..\\..\\Users\\Public\\probe.exe"), true, &ignored, &err),
             QStringLiteral("OrionNative.exe"));
    QVERIFY(ignored);
    QCOMPARE(relaunchExecutableName(QString(), true, &ignored, &err), QStringLiteral("OrionNative.exe"));
    QVERIFY(!ignored);
    QCOMPARE(relaunchExecutableName(QStringLiteral("OrionNative.exe"), true, &ignored, &err), QStringLiteral("OrionNative.exe"));
    QVERIFY(!ignored);
    // Development: bare .exe names only.
    QCOMPARE(relaunchExecutableName(QString(), false, nullptr, &err), QStringLiteral("OrionNative.exe"));
    QCOMPARE(relaunchExecutableName(QStringLiteral("OrionOwner.exe"), false, nullptr, &err), QStringLiteral("OrionOwner.exe"));
    for (const QString& bad : {QStringLiteral("..\\probe.exe"), QStringLiteral("../probe.exe"),
                               QStringLiteral("sub/probe.exe"), QStringLiteral("C:\\probe.exe"),
                               QStringLiteral("probe.exe:stream"), QStringLiteral(".probe.exe"),
                               QStringLiteral("probe.dll"), QStringLiteral("probe")}) {
        QVERIFY2(relaunchExecutableName(bad, false, nullptr, &err).isEmpty(), qPrintable(bad));
    }
}

void OrionUpdaterTests::safeRelaunchPathRefusesLinksAndEscapes()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    QVERIFY(QDir().mkpath(install) && QDir().mkpath(outside));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("exe")));
    QVERIFY(writeTextFile(outside + QStringLiteral("/probe.exe"), QByteArrayLiteral("probe")));
    QString err;
    QCOMPARE(safeRelaunchPath(install, QStringLiteral("OrionNative.exe"), &err),
             QDir::cleanPath(install + QStringLiteral("/OrionNative.exe")));
    QVERIFY(safeRelaunchPath(install, QStringLiteral("missing.exe"), &err).isEmpty());
    QVERIFY(safeRelaunchPath(install, QStringLiteral("../outside/probe.exe"), &err).isEmpty());
    // A file symlink or a junctioned component inside the install dir is refused too.
    ORION_REQUIRE_LINK(makeJunction(install + QStringLiteral("/bin"), outside));
    QVERIFY(safeRelaunchPath(install, QStringLiteral("bin/probe.exe"), &err).isEmpty());
}

void OrionUpdaterTests::extractRefusesPreexistingJunctionStagingRoot()
{
    // [Codex r3 F2] The staging root is judged BEFORE it is cleared: a junction root would
    // otherwise be removed THROUGH, deleting the outside tree.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    QVERIFY(QDir().mkpath(outside));
    QVERIFY(writeTextFile(outside + QStringLiteral("/sentinel.txt"), QByteArrayLiteral("keep me")));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    ORION_REQUIRE_LINK(makeJunction(stage, outside));
    const QString zipPath = tmp.filePath(QStringLiteral("ok.zip"));
    QVERIFY(writeStoredZip(zipPath, QStringLiteral("a.txt"), QByteArrayLiteral("a")));
    const ExtractResult result = extractZipSafely(zipPath, stage);
    QVERIFY(!result.ok);
    QVERIFY2(result.error.contains(QStringLiteral("reparse")), qPrintable(result.error));
    QVERIFY(QFile::exists(outside + QStringLiteral("/sentinel.txt")));
    QVERIFY(!QFile::exists(outside + QStringLiteral("/a.txt")));
}

void OrionUpdaterTests::backupRefusesPreexistingJunctionBackupRoot()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    QVERIFY(QDir().mkpath(install) && QDir().mkpath(outside));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(outside + QStringLiteral("/sentinel.txt"), QByteArrayLiteral("keep me")));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    ORION_REQUIRE_LINK(makeJunction(backup, outside));
    QString err;
    QVERIFY(!backupTree(install, backup, &err));
    QVERIFY2(err.contains(QStringLiteral("reparse")), qPrintable(err));
    QVERIFY(QFile::exists(outside + QStringLiteral("/sentinel.txt")));   // never cleared through
    QVERIFY(!QFile::exists(outside + QStringLiteral("/OrionNative.exe")));
}

void OrionUpdaterTests::restoreTreeIsExactWhenThePlantedJunctionIsStillThere()
{
    // [Codex r3 F4] The junction that broke the apply is STILL in the install tree when
    // the rollback runs. Exact restore removes the link itself (never following it), so
    // the partial update does not survive and the outside target is untouched.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/qml")) && QDir().mkpath(outside));
    QVERIFY(QDir().mkpath(stage + QStringLiteral("/plugins")));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/qml/Main.qml"), QByteArrayLiteral("qml-v1")));
    QVERIFY(writeTextFile(outside + QStringLiteral("/sentinel.txt"), QByteArrayLiteral("keep me")));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));
    ORION_REQUIRE_LINK(makeJunction(install + QStringLiteral("/plugins"), outside));
    QVERIFY(writeTextFile(stage + QStringLiteral("/Extra.dll"), QByteArrayLiteral("new")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v2")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/plugins/evil.dll"), QByteArrayLiteral("pwned")));

    QVERIFY(!applyTree(stage, install, nullptr, &err));
    QVERIFY(QFile::exists(install + QStringLiteral("/Extra.dll")));
    QVERIFY(isReparsePointPath(install + QStringLiteral("/plugins")));      // still planted
    QVERIFY2(restoreTree(backup, install, &err), qPrintable(err));          // exact restore anyway

    QVERIFY(!QFile::exists(install + QStringLiteral("/Extra.dll")));
    QVERIFY(!QFileInfo::exists(install + QStringLiteral("/plugins")));      // link removed...
    QVERIFY(QFile::exists(outside + QStringLiteral("/sentinel.txt")));      // ...target untouched
    QVERIFY(!QFile::exists(outside + QStringLiteral("/evil.dll")));
    QFile exe(install + QStringLiteral("/OrionNative.exe"));
    QVERIFY(exe.open(QIODevice::ReadOnly));
    QCOMPARE(exe.readAll(), QByteArrayLiteral("v1"));
    QStringList installFiles, backupFiles;
    for (QDirIterator it(install, QDir::Files, QDirIterator::Subdirectories); it.hasNext();) {
        installFiles << QDir(install).relativeFilePath(it.next());
    }
    for (QDirIterator it(backup, QDir::Files, QDirIterator::Subdirectories); it.hasNext();) {
        backupFiles << QDir(backup).relativeFilePath(it.next());
    }
    installFiles.sort();
    backupFiles.sort();
    QCOMPARE(installFiles, backupFiles);
}

void OrionUpdaterTests::removeTreeSafelyUnlinksAChildJunctionWithoutFollowing()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString tree = tmp.filePath(QStringLiteral("tree"));
    QVERIFY(QDir().mkpath(outside + QStringLiteral("/deep")) && QDir().mkpath(tree + QStringLiteral("/real/sub")));
    QVERIFY(writeTextFile(outside + QStringLiteral("/sentinel.txt"), QByteArrayLiteral("keep me")));
    QVERIFY(writeTextFile(outside + QStringLiteral("/deep/also.txt"), QByteArrayLiteral("keep me too")));
    QVERIFY(writeTextFile(tree + QStringLiteral("/real/sub/file.txt"), QByteArrayLiteral("mine")));
    ORION_REQUIRE_LINK(makeJunction(tree + QStringLiteral("/link"), outside));
    ORION_REQUIRE_LINK(makeJunction(tree + QStringLiteral("/real/nested_link"), outside));
    QString err;
    QVERIFY2(removeTreeSafely(tree, &err), qPrintable(err));
    QVERIFY(!QFileInfo::exists(tree));
    QVERIFY(QFile::exists(outside + QStringLiteral("/sentinel.txt")));
    QVERIFY(QFile::exists(outside + QStringLiteral("/deep/also.txt")));
    QVERIFY2(removeTreeSafely(tree, &err), qPrintable(err));   // idempotent on a missing tree
}

void OrionUpdaterTests::extractAndBackupClearRootsWithoutFollowingChildJunctions()
{
    // [Codex r4 F2] A junction INSIDE an existing staging/backup root must be unlinked,
    // not traversed, when the root is cleared before use.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    QVERIFY(QDir().mkpath(outside));
    QVERIFY(writeTextFile(outside + QStringLiteral("/sentinel.txt"), QByteArrayLiteral("keep me")));

    const QString stage = tmp.filePath(QStringLiteral("stage"));
    QVERIFY(QDir().mkpath(stage));
    ORION_REQUIRE_LINK(makeJunction(stage + QStringLiteral("/link"), outside));
    const QString zipPath = tmp.filePath(QStringLiteral("ok.zip"));
    QVERIFY(writeStoredZip(zipPath, QStringLiteral("a.txt"), QByteArrayLiteral("a")));
    const ExtractResult result = extractZipSafely(zipPath, stage);
    QVERIFY2(result.ok, qPrintable(result.error));
    QVERIFY(QFile::exists(outside + QStringLiteral("/sentinel.txt")));
    QVERIFY(!QFileInfo::exists(stage + QStringLiteral("/link")));
    QVERIFY(QFile::exists(stage + QStringLiteral("/a.txt")));

    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QVERIFY(QDir().mkpath(install) && QDir().mkpath(backup));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    ORION_REQUIRE_LINK(makeJunction(backup + QStringLiteral("/link"), outside));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));
    QVERIFY(QFile::exists(outside + QStringLiteral("/sentinel.txt")));
    QVERIFY(!QFileInfo::exists(backup + QStringLiteral("/link")));
    QVERIFY(QFile::exists(backup + QStringLiteral("/OrionNative.exe")));
}

void OrionUpdaterTests::restoreReplacesAJunctionThatShadowsABackedUpDirectory()
{
    // [Codex r4 F4] The planted junction sits where a backed-up DIRECTORY belongs.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/plugins")) && QDir().mkpath(outside) && QDir().mkpath(stage));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/plugins/original.dll"), QByteArrayLiteral("orig")));
    QVERIFY(writeTextFile(outside + QStringLiteral("/sentinel.txt"), QByteArrayLiteral("keep me")));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));
    // Replace the real plugins dir with a junction to outside, then a partial apply.
    QVERIFY(QDir(install + QStringLiteral("/plugins")).removeRecursively());
    ORION_REQUIRE_LINK(makeJunction(install + QStringLiteral("/plugins"), outside));
    QVERIFY(writeTextFile(stage + QStringLiteral("/Extra.dll"), QByteArrayLiteral("new")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v2")));
    QVERIFY(QDir().mkpath(stage + QStringLiteral("/plugins")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/plugins/evil.dll"), QByteArrayLiteral("pwned")));
    QVERIFY(!applyTree(stage, install, nullptr, &err));                  // refused at the junction
    QVERIFY(isReparsePointPath(install + QStringLiteral("/plugins")));   // still planted

    QVERIFY2(restoreTree(backup, install, &err), qPrintable(err));       // exact restore anyway
    QVERIFY(!isReparsePointPath(install + QStringLiteral("/plugins")));
    QVERIFY(QFileInfo(install + QStringLiteral("/plugins")).isDir());
    QFile orig(install + QStringLiteral("/plugins/original.dll"));
    QVERIFY(orig.open(QIODevice::ReadOnly));
    QCOMPARE(orig.readAll(), QByteArrayLiteral("orig"));
    QVERIFY(!QFile::exists(install + QStringLiteral("/Extra.dll")));
    QVERIFY(QFile::exists(outside + QStringLiteral("/sentinel.txt")));
    QVERIFY(!QFile::exists(outside + QStringLiteral("/original.dll")));  // never written through
    QVERIFY(!QFile::exists(outside + QStringLiteral("/evil.dll")));
    QFile exe(install + QStringLiteral("/OrionNative.exe"));
    QVERIFY(exe.open(QIODevice::ReadOnly));
    QCOMPARE(exe.readAll(), QByteArrayLiteral("v1"));
}

void OrionUpdaterTests::extendedLengthPathHandlesDriveAndUncForms()
{
#if defined(Q_OS_WIN)
    const QString ext = QStringLiteral("\\\\?\\");
    const QString extUnc = QStringLiteral("\\\\?\\UNC\\");
    // Drive path -> \\?\C:\...
    const QString drive = toExtendedLengthPath(QStringLiteral("C:/Program Files/Venice/plugins"));
    QVERIFY2(drive.startsWith(ext + QStringLiteral("C:")), qPrintable(drive));
    QVERIFY(!drive.startsWith(extUnc));
    // UNC path -> \\?\UNC\server\share\..., never \\?\server\share
    const QString unc = toExtendedLengthPath(QStringLiteral("//server/share/Venice/plugins"));
    QVERIFY2(unc.startsWith(extUnc + QStringLiteral("server") + QLatin1Char('\\') + QStringLiteral("share")), qPrintable(unc));
    // Idempotent on both extended forms.
    QCOMPARE(toExtendedLengthPath(drive), drive);
    QCOMPARE(toExtendedLengthPath(unc), unc);
#else
    QSKIP("Win32 path forms");
#endif
}

void OrionUpdaterTests::extractRefusesTruncatedEntry()
{
    // [Codex r5] The central directory says 40 bytes; the entry carries 5. A truncated
    // download must not become an installed executable.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString zipPath = tmp.filePath(QStringLiteral("short.zip"));
    QVERIFY(writeStoredZipDeclaring(zipPath, QStringLiteral("OrionNative.exe"), QByteArrayLiteral("trunc"), 40));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    const ExtractResult result = extractZipSafely(zipPath, stage);
    QVERIFY(!result.ok);
    QVERIFY2(result.error.contains(QStringLiteral("truncated")), qPrintable(result.error));
    QCOMPARE(result.rejectedEntry, QStringLiteral("OrionNative.exe"));
    QVERIFY(!QFile::exists(stage + QStringLiteral("/OrionNative.exe")));   // wiped
    // A consistent entry of the same shape extracts fine (the check is exact, not paranoid).
    const QString okZip = tmp.filePath(QStringLiteral("ok.zip"));
    QVERIFY(writeStoredZipDeclaring(okZip, QStringLiteral("OrionNative.exe"), QByteArrayLiteral("trunc"), 5));
    QVERIFY(extractZipSafely(okZip, tmp.filePath(QStringLiteral("stage2"))).ok);
}

void OrionUpdaterTests::restoreReplacesAFileSymlinkThatShadowsABackedUpFile()
{
    // [Codex r5 F4] The planted link is a FILE symlink sitting where a backed-up file belongs.
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString outside = tmp.filePath(QStringLiteral("outside"));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QVERIFY(QDir().mkpath(install) && QDir().mkpath(outside));
    QVERIFY(writeTextFile(install + QStringLiteral("/Orion.dll"), QByteArrayLiteral("orig")));
    QVERIFY(writeTextFile(outside + QStringLiteral("/target.dll"), QByteArrayLiteral("attacker")));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));
    QVERIFY(QFile::remove(install + QStringLiteral("/Orion.dll")));
    if (!makeFileSymlink(install + QStringLiteral("/Orion.dll"), outside + QStringLiteral("/target.dll"))) {
        QSKIP("file symlinks need SeCreateSymbolicLinkPrivilege or Developer Mode on this account");
    }
    QVERIFY(isReparsePointPath(install + QStringLiteral("/Orion.dll")));
    QVERIFY2(restoreTree(backup, install, &err), qPrintable(err));
    QVERIFY(!isReparsePointPath(install + QStringLiteral("/Orion.dll")));
    QFile restored(install + QStringLiteral("/Orion.dll"));
    QVERIFY(restored.open(QIODevice::ReadOnly));
    QCOMPARE(restored.readAll(), QByteArrayLiteral("orig"));
    QFile target(outside + QStringLiteral("/target.dll"));
    QVERIFY(target.open(QIODevice::ReadOnly));
    QCOMPARE(target.readAll(), QByteArrayLiteral("attacker"));   // never written through
}

void OrionUpdaterTests::stagedRuntimeClosureIsCompleteAndOutsideInstall()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("Venice"));
    const QString stage = tmp.filePath(QStringLiteral(".orion_updater_stage-test"));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/platforms")));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/tls")));
    const QStringList closure = {
        QStringLiteral("OrionUpdater.exe"), QStringLiteral("UpdaterCore.dll"),
        QStringLiteral("SecurityCore.dll"), QStringLiteral("OrionCommon.dll"),
        QStringLiteral("Qt6Core.dll"), QStringLiteral("Qt6Gui.dll"),
        QStringLiteral("Qt6Network.dll"), QStringLiteral("Qt6Widgets.dll"),
        QStringLiteral("platforms/qwindows.dll"), QStringLiteral("tls/qschannelbackend.dll")};
    QJsonObject files;
    for (const QString& rel : closure) {
        const QByteArray bytes = rel.toUtf8() + QByteArrayLiteral("-old");
        QVERIFY(writeTextFile(install + QLatin1Char('/') + rel, bytes));
        files.insert(rel, QJsonObject{{QStringLiteral("sha256"), QString::fromLatin1(
            QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex())}});
    }
    QVERIFY(writeTextFile(install + QStringLiteral("/customer-data.txt"), QByteArrayLiteral("private")));
    QString error;
    QCOMPARE(bindStagedInstallRoot(install, stage, true, &error), QDir::cleanPath(install));
    QVERIFY2(stageUpdaterRuntime(install, stage, files, &error), qPrintable(error));
    QVERIFY2(verifyStagedUpdaterRuntime(install, stage, files, &error), qPrintable(error));
    for (const QString& rel : closure) {
        QFile copied(stage + QLatin1Char('/') + rel);
        QVERIFY(copied.open(QIODevice::ReadOnly));
        QCOMPARE(copied.readAll(), rel.toUtf8() + QByteArrayLiteral("-old"));
    }
    QVERIFY(!QFileInfo::exists(stage + QStringLiteral("/customer-data.txt")));
    const QString foreign = tmp.filePath(QStringLiteral("foreign-stage"));
    QVERIFY(bindStagedInstallRoot(install, foreign, true, &error).isEmpty());
    QVERIFY(QFile::remove(stage + QStringLiteral("/Qt6Network.dll")));
    QVERIFY(!verifyStagedUpdaterRuntime(install, stage, files, &error));
}

void OrionUpdaterTests::retiredManifestedFilesArePrunedButUserDataSurvivesRollback()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    const QString backup = tmp.filePath(QStringLiteral("backup"));
    const QString stage = tmp.filePath(QStringLiteral("stage"));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/plugins")));
    QVERIFY(QDir().mkpath(stage));
    QVERIFY(writeTextFile(install + QStringLiteral("/plugins/retired.dll"), QByteArrayLiteral("old-dll")));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("old-exe")));
    QVERIFY(writeTextFile(install + QStringLiteral("/customer-data.txt"), QByteArrayLiteral("user-data")));
    QVERIFY(writeTextFile(stage + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("new-exe")));
    QJsonObject oldFiles{{QStringLiteral("plugins/retired.dll"), QStringLiteral("old-hash")},
                         {QStringLiteral("OrionNative.exe"), QStringLiteral("old-hash")}};
    QJsonObject newFiles{{QStringLiteral("OrionNative.exe"), QStringLiteral("new-hash")}};
    QString error;
    QVERIFY2(backupTree(install, backup, &error), qPrintable(error));
    QVERIFY2(applyTree(stage, install, nullptr, &error), qPrintable(error));
    QVERIFY2(pruneRetiredRuntimeFiles(install, oldFiles, newFiles, &error), qPrintable(error));
    QVERIFY(!QFileInfo::exists(install + QStringLiteral("/plugins/retired.dll")));
    QFile user(install + QStringLiteral("/customer-data.txt"));
    QVERIFY(user.open(QIODevice::ReadOnly));
    QCOMPARE(user.readAll(), QByteArrayLiteral("user-data"));
    user.close();
    // Inject a post-prune validation fault. The same exact-restore path used by
    // the updater must recover the retired entry as well as overwritten bytes.
    QVERIFY2(restoreTree(backup, install, &error), qPrintable(error));
    QFile retired(install + QStringLiteral("/plugins/retired.dll"));
    QVERIFY(retired.open(QIODevice::ReadOnly));
    QCOMPARE(retired.readAll(), QByteArrayLiteral("old-dll"));
    QFile executable(install + QStringLiteral("/OrionNative.exe"));
    QVERIFY(executable.open(QIODevice::ReadOnly));
    QCOMPARE(executable.readAll(), QByteArrayLiteral("old-exe"));
}

QTEST_MAIN(OrionUpdaterTests)

#include "OrionUpdaterTests.moc"
