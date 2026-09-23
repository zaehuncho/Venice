// [SERVER-SHARD blocker #5, Codex finding #1/#2 — 2026-09-20 round 2]
// Manifest-trust matrix for the broker install-trust gate
// (orion::releaseManifestCoversFileSigned / genuineBrokerInstallPresent).
//
// Only a CORRECTLY SIGNED release_manifest.json — pinned public_key_id, a detached
// Ed25519 signature that verifies over the EXACT manifest bytes, customer audience,
// signature_required — that also covers the target file by an exact SHA-256 is
// trusted. Unsigned / wrong-key / bad-signature / wrong-key-id / wrong-audience /
// hash-mismatch all fail closed; only the last case passes.
//
// The signing key is EPHEMERAL and generated in-test — never a real key. It is
// wired in through the DEV-ONLY release-manifest test-override path
// (selectReleaseManifestPublicKeyEncoding), so this never touches the pinned
// production key and productionBuild=false is required for the override to apply.

#include <QtCore/QByteArray>
#include <QtCore/QCryptographicHash>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QTemporaryDir>
#include <QtTest/QtTest>

#include "BrokerInstallTrust.h"
#include "Ed25519.h"
#include "ReleaseManifestTrust.h"

using namespace orion;

namespace {

QByteArray sha256Hex(const QByteArray& b)
{
    return QCryptographicHash::hash(b, QCryptographicHash::Sha256).toHex();
}

// Build a release_manifest.json body covering `targetName` by `targetHashHex`.
QByteArray buildManifest(const QString& keyId, bool signatureRequired,
                         const QString& audience, const QString& targetName,
                         const QByteArray& targetHashHex, qint64 targetSize)
{
    QJsonObject fileEntry;
    fileEntry.insert(QStringLiteral("sha256"), QString::fromLatin1(targetHashHex));
    fileEntry.insert(QStringLiteral("size"), targetSize);
    QJsonObject files;
    files.insert(targetName, fileEntry);
    QJsonObject m;
    m.insert(QStringLiteral("schema"), QStringLiteral("orion.release_manifest.v1"));
    m.insert(QStringLiteral("audience"), audience);
    m.insert(QStringLiteral("file_count"), 1);
    m.insert(QStringLiteral("files"), files);
    m.insert(QStringLiteral("public_key_id"), keyId);
    m.insert(QStringLiteral("signature_alg"), QStringLiteral("ed25519"));
    m.insert(QStringLiteral("signature_required"), signatureRequired);
    return QJsonDocument(m).toJson(QJsonDocument::Compact);
}

} // namespace

class BrokerInstallTrustTests : public QObject
{
    Q_OBJECT

    QByteArray seedA_, seedB_, pubA_, pubB_;

    static void writeFile(const QString& path, const QByteArray& bytes)
    {
        QFile f(path);
        if (f.open(QIODevice::WriteOnly)) {
            f.write(bytes);
            f.close();
        }
    }

    // Lay out targetDir with the target exe, manifest and (optional) detached sig.
    // Returns the absolute target path.
    QString layout(const QTemporaryDir& dir, const QByteArray& manifestBytes,
                   const QByteArray* sigEncodedOrNull, const QByteArray& targetBytes)
    {
        const QString targetPath = dir.path() + QStringLiteral("/OrionActivate.exe");
        writeFile(targetPath, targetBytes);
        writeFile(dir.path() + QStringLiteral("/release_manifest.json"), manifestBytes);
        if (sigEncodedOrNull) {
            writeFile(dir.path() + QStringLiteral("/release_manifest.sig"), *sigEncodedOrNull);
        }
        return targetPath;
    }

private slots:
    void initTestCase()
    {
        QVERIFY2(ed25519Available(),
                 "libcrypto with Ed25519 must be loadable for the manifest-trust tests");
        seedA_ = QByteArray(32, '\x11');
        seedB_ = QByteArray(32, '\x22');
        pubA_ = ed25519DerivePublicKey(seedA_);
        pubB_ = ed25519DerivePublicKey(seedB_);
        QCOMPARE(pubA_.size(), 32);
        QCOMPARE(pubB_.size(), 32);
        QVERIFY(pubA_ != pubB_);
    }

    // The ONLY case that passes: a valid signature by the (test-overridden) pinned
    // key over the exact bytes, customer audience, covering the file by hash.
    void correctSignedManifestPasses()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray target = "MZ-fake-broker-bytes-0123456789";
        const QByteArray manifest = buildManifest(
            QStringLiteral("orion-ed25519-v1"), true, QStringLiteral("customer"),
            QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
        const QByteArray sig = ed25519Sign(seedA_, manifest).toHex();
        const QString targetPath = layout(dir, manifest, &sig, target);

        QString detail;
        QVERIFY2(releaseManifestCoversFileSigned(
                     dir.path(), targetPath, /*productionBuild=*/false,
                     QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), &detail),
                 qPrintable(detail));

        // The combined guard trusts it via gate B (the file isn't Authenticode-signed).
        QVERIFY(genuineBrokerInstallPresent(
            targetPath, dir.path(), /*productionBuild=*/false,
            QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), nullptr));
    }

    // [FIX B — 2026-09-20 hardening round 3] With kExpectedBrokerSignerThumbprint
    // EMPTY (the shipped state) the Authenticode gate (A) is INERT — it trusts
    // NOTHING on its own, so the signed release manifest is the SOLE anchor. A
    // genuinely trusted / matching-subject exe with NO manifest coverage must FAIL;
    // only the correctly Ed25519-signed manifest (ephemeral key) passes.
    void authenticodeGateInertWhileThumbprintEmpty()
    {
        // Meaningful only while the compiled-in thumbprint is empty. If the owner
        // later pins a real leaf thumbprint, gate (A) becomes active — skip then.
        const QString pinned =
            QString::fromWCharArray(kExpectedBrokerSignerThumbprint).trimmed();
        if (!pinned.isEmpty()) {
            QSKIP("owner has pinned a leaf thumbprint; gate (A) is active");
        }

        // (1) A validly Authenticode-signed system exe still FAILS gate (A): the
        // gate is inert without a pinned thumbprint, regardless of real trust state.
        const QString signedSystemExe =
            QStringLiteral("C:/Windows/System32/notepad.exe");
        if (QFileInfo::exists(signedSystemExe)) {
            QString detail;
            QVERIFY2(!fileAuthenticodeSignedByVenice(signedSystemExe, &detail),
                     qPrintable(detail));
        }

        // (2) genuineBrokerInstallPresent on an exe with NO manifest coverage FAILS:
        // gate (A) is inert and gate (B) has nothing to verify against.
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray target = "MZ-broker-no-manifest";
        const QString targetPath = dir.path() + QStringLiteral("/OrionActivate.exe");
        writeFile(targetPath, target);
        QString detail;
        QVERIFY2(!fileAuthenticodeSignedByVenice(targetPath, &detail), qPrintable(detail));
        QVERIFY2(!genuineBrokerInstallPresent(
                     targetPath, dir.path(), /*productionBuild=*/false,
                     QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), &detail),
                 qPrintable(detail));

        // (3) The correctly Ed25519-signed manifest fixture (ephemeral key) PASSES
        // via gate (B) — proving the signed manifest is a sufficient sole anchor.
        const QByteArray manifest = buildManifest(
            QStringLiteral("orion-ed25519-v1"), true, QStringLiteral("customer"),
            QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
        const QByteArray sig = ed25519Sign(seedA_, manifest).toHex();
        writeFile(dir.path() + QStringLiteral("/release_manifest.json"), manifest);
        writeFile(dir.path() + QStringLiteral("/release_manifest.sig"), sig);
        QVERIFY2(genuineBrokerInstallPresent(
                     targetPath, dir.path(), /*productionBuild=*/false,
                     QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), &detail),
                 qPrintable(detail));
    }

    void unsignedManifestFails()
    {
        // (a) signature_required:false — never trusted, even with a valid sig present.
        {
            QTemporaryDir dir;
            QVERIFY(dir.isValid());
            const QByteArray target = "broker-a";
            const QByteArray manifest = buildManifest(
                QStringLiteral("orion-ed25519-v1"), false, QStringLiteral("customer"),
                QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
            const QByteArray sig = ed25519Sign(seedA_, manifest).toHex();
            const QString targetPath = layout(dir, manifest, &sig, target);
            QVERIFY(!releaseManifestCoversFileSigned(
                dir.path(), targetPath, false,
                QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), nullptr));
        }
        // (b) signature_required:true but NO detached signature file at all.
        {
            QTemporaryDir dir;
            QVERIFY(dir.isValid());
            const QByteArray target = "broker-b";
            const QByteArray manifest = buildManifest(
                QStringLiteral("orion-ed25519-v1"), true, QStringLiteral("customer"),
                QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
            const QString targetPath = layout(dir, manifest, nullptr, target);
            QVERIFY(!releaseManifestCoversFileSigned(
                dir.path(), targetPath, false,
                QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), nullptr));
        }
    }

    void wrongKeyFails()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray target = "broker-c";
        const QByteArray manifest = buildManifest(
            QStringLiteral("orion-ed25519-v1"), true, QStringLiteral("customer"),
            QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
        const QByteArray sig = ed25519Sign(seedA_, manifest).toHex();  // signed by A
        const QString targetPath = layout(dir, manifest, &sig, target);
        // Verified against B's public key -> InvalidSignature.
        QVERIFY(!releaseManifestCoversFileSigned(
            dir.path(), targetPath, false,
            QStringLiteral("orion-ed25519-v1"), pubB_.toBase64(), nullptr));
    }

    void badSignatureFails()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray target = "broker-d";
        const QByteArray manifest = buildManifest(
            QStringLiteral("orion-ed25519-v1"), true, QStringLiteral("customer"),
            QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
        QByteArray sig = ed25519Sign(seedA_, manifest).toHex();
        sig[0] = (sig[0] == '0') ? '1' : '0';  // corrupt one nibble
        const QString targetPath = layout(dir, manifest, &sig, target);
        QVERIFY(!releaseManifestCoversFileSigned(
            dir.path(), targetPath, false,
            QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), nullptr));
    }

    void wrongPublicKeyIdFails()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray target = "broker-e";
        // public_key_id is NOT the pinned id -> rejected even with a valid signature.
        const QByteArray manifest = buildManifest(
            QStringLiteral("orion-ed25519-v2"), true, QStringLiteral("customer"),
            QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
        const QByteArray sig = ed25519Sign(seedA_, manifest).toHex();
        const QString targetPath = layout(dir, manifest, &sig, target);
        // Overriding v1 doesn't help (manifest id != pinned id).
        QVERIFY(!releaseManifestCoversFileSigned(
            dir.path(), targetPath, false,
            QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), nullptr));
        // Overriding v2 also fails: the gate requires the PINNED id, not any id.
        QVERIFY(!releaseManifestCoversFileSigned(
            dir.path(), targetPath, false,
            QStringLiteral("orion-ed25519-v2"), pubA_.toBase64(), nullptr));
    }

    void wrongAudienceFails()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray target = "broker-f";
        const QByteArray manifest = buildManifest(
            QStringLiteral("orion-ed25519-v1"), true, QStringLiteral("internal"),
            QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
        const QByteArray sig = ed25519Sign(seedA_, manifest).toHex();
        const QString targetPath = layout(dir, manifest, &sig, target);
        QVERIFY(!releaseManifestCoversFileSigned(
            dir.path(), targetPath, false,
            QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), nullptr));
    }

    void validSignatureButHashMismatchFails()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray hashed = "broker-original-bytes";
        // Manifest hashes `hashed`, but the file on disk is different bytes.
        const QByteArray manifest = buildManifest(
            QStringLiteral("orion-ed25519-v1"), true, QStringLiteral("customer"),
            QStringLiteral("OrionActivate.exe"), sha256Hex(hashed), hashed.size());
        const QByteArray sig = ed25519Sign(seedA_, manifest).toHex();
        const QString targetPath = layout(dir, manifest, &sig, QByteArray("TAMPERED-BYTES"));
        // Signature verifies, but coverage (SHA-256 of the actual file) mismatches.
        QVERIFY(!releaseManifestCoversFileSigned(
            dir.path(), targetPath, false,
            QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), nullptr));
    }

    void productionIgnoresTestOverride()
    {
        // With productionBuild=true the dev test override is ignored, so the
        // ephemeral key can never authorize — even a perfectly signed manifest is
        // rejected because the real pinned key won't match this test signature.
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray target = "broker-g";
        const QByteArray manifest = buildManifest(
            QStringLiteral("orion-ed25519-v1"), true, QStringLiteral("customer"),
            QStringLiteral("OrionActivate.exe"), sha256Hex(target), target.size());
        const QByteArray sig = ed25519Sign(seedA_, manifest).toHex();
        const QString targetPath = layout(dir, manifest, &sig, target);
        QVERIFY(!releaseManifestCoversFileSigned(
            dir.path(), targetPath, /*productionBuild=*/true,
            QStringLiteral("orion-ed25519-v1"), pubA_.toBase64(), nullptr));
    }
};

QTEST_GUILESS_MAIN(BrokerInstallTrustTests)
#include "BrokerInstallTrustTests.moc"
