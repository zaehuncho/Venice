#include "../src/Diagnostics.h"
#include "../src/Ed25519.h"
#include "../src/LicenseClient.h"     // compareSemanticVersions
#include "../src/UpdateManifest.h"
#include "../src/UpdaterArchive.h"

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
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString install = tmp.filePath(QStringLiteral("install"));
    QVERIFY(QDir().mkpath(install + QStringLiteral("/qml")));
    QVERIFY(writeTextFile(install + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v1")));
    QVERIFY(writeTextFile(install + QStringLiteral("/qml/Main.qml"), QByteArrayLiteral("qml-v1")));

    const QString backup = tmp.filePath(QStringLiteral("backup"));
    QString err;
    QVERIFY2(backupTree(install, backup, &err), qPrintable(err));

    // Apply a partial/broken new build.
    const QString staged = tmp.filePath(QStringLiteral("staged"));
    QVERIFY(QDir().mkpath(staged));
    QVERIFY(writeTextFile(staged + QStringLiteral("/OrionNative.exe"), QByteArrayLiteral("v2-broken")));
    QVERIFY2(applyTree(staged, install, nullptr, &err), qPrintable(err));

    // Failure detected post-apply -> roll back.
    QVERIFY2(restoreTree(backup, install, &err), qPrintable(err));

    QFile exe(install + QStringLiteral("/OrionNative.exe"));
    QVERIFY(exe.open(QIODevice::ReadOnly));
    QCOMPARE(exe.readAll(), QByteArrayLiteral("v1"));
    QFile qml(install + QStringLiteral("/qml/Main.qml"));
    QVERIFY(qml.open(QIODevice::ReadOnly));
    QCOMPARE(qml.readAll(), QByteArrayLiteral("qml-v1"));
}

QTEST_MAIN(OrionUpdaterTests)

#include "OrionUpdaterTests.moc"
