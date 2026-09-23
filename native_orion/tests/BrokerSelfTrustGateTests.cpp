// [SERVER-SHARD blocker #5, FIX A — 2026-09-20 hardening round 3]
// Broker self-trust ENFORCEMENT gate.
//
// The broker consults orion::broker::planBrokerActions(mode, selfTrusted) FIRST in
// wWinMain, before it touches the registry, the network, DPAPI, or launches
// anything. Each BrokerActionPlan flag maps 1:1 to a side effect there:
//   registerHandler -> the orion:// HKCU registry write
//   runActivation   -> the WinHTTP activation + DPAPI session write + CreateProcess
//   launchOrPrompt  -> CreateProcess of an existing session (or the connect prompt)
// So a plan with `blocked` set and every flag clear is a faithful proof that a
// non-genuine broker makes NO registry change, NO WinHTTP request, NO DPAPI/session
// write, and NO CreateProcess, and exits NONZERO.
//
// This suite drives the REAL shared trust TU (orion::genuineBrokerInstallPresent,
// SecurityCore) to PRODUCE both false verdicts (tampered self / missing sig / blank
// sig) and a true verdict (a correctly Ed25519-signed manifest, EPHEMERAL in-test
// key), then feeds each verdict into the planner for all three entry modes. The
// libcrypto-unavailable cause is a code-review invariant: releaseManifestCoversFileSigned
// returns false when !ed25519Available(); it cannot be injected deterministically
// here because these tests REQUIRE libcrypto to build the signed fixtures, so
// initTestCase asserts libcrypto is present and that branch stays covered by review.

#include <QtCore/QByteArray>
#include <QtCore/QCryptographicHash>
#include <QtCore/QFile>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QString>
#include <QtCore/QTemporaryDir>
#include <QtTest/QtTest>

#include "../broker/BrokerContract.h"
#include "BrokerInstallTrust.h"
#include "Ed25519.h"
#include "ReleaseManifestTrust.h"

using namespace orion;
using orion::broker::BrokerActionPlan;
using orion::broker::BrokerInvocation;
using orion::broker::kBrokerUntrustedExit;
using orion::broker::planBrokerActions;

namespace {

QByteArray sha256Hex(const QByteArray& b)
{
    return QCryptographicHash::hash(b, QCryptographicHash::Sha256).toHex();
}

// A correctly-shaped release manifest covering `targetName` by `targetHashHex`.
QByteArray buildManifest(const QString& targetName, const QByteArray& targetHashHex,
                         qint64 targetSize)
{
    QJsonObject fileEntry;
    fileEntry.insert(QStringLiteral("sha256"), QString::fromLatin1(targetHashHex));
    fileEntry.insert(QStringLiteral("size"), targetSize);
    QJsonObject files;
    files.insert(targetName, fileEntry);
    QJsonObject m;
    m.insert(QStringLiteral("schema"), QStringLiteral("orion.release_manifest.v1"));
    m.insert(QStringLiteral("audience"), QStringLiteral("customer"));
    m.insert(QStringLiteral("file_count"), 1);
    m.insert(QStringLiteral("files"), files);
    m.insert(QStringLiteral("public_key_id"), QStringLiteral("orion-ed25519-v1"));
    m.insert(QStringLiteral("signature_alg"), QStringLiteral("ed25519"));
    m.insert(QStringLiteral("signature_required"), true);
    return QJsonDocument(m).toJson(QJsonDocument::Compact);
}

void writeFile(const QString& path, const QByteArray& bytes)
{
    QFile f(path);
    if (f.open(QIODevice::WriteOnly)) {
        f.write(bytes);
        f.close();
    }
}

const BrokerInvocation kAllModes[] = {
    BrokerInvocation::Register,
    BrokerInvocation::UriActivation,
    BrokerInvocation::SessionLaunch,
};

// Every side-effect flag clear + blocked + nonzero exit == the fail-closed verdict.
void assertFullyBlocked(const BrokerActionPlan& plan)
{
    QVERIFY(plan.blocked);
    QVERIFY(!plan.registerHandler);  // NO registry change
    QVERIFY(!plan.runActivation);    // NO WinHTTP request + NO DPAPI/session write
    QVERIFY(!plan.launchOrPrompt);   // NO CreateProcess
    QVERIFY2(plan.exitCode != 0, "a blocked broker must exit nonzero");
    QCOMPARE(plan.exitCode, kBrokerUntrustedExit);
}

} // namespace

class BrokerSelfTrustGateTests : public QObject
{
    Q_OBJECT

    QByteArray seed_, pub_;

    // Lay out targetDir with target exe + signed manifest (+/- sig) using the
    // ephemeral key, and return the shared trust TU's verdict for that exe.
    bool genuineVerdictFor(const QTemporaryDir& dir, const QByteArray& targetBytes,
                           const QByteArray& hashedBytes, bool writeSig, bool blankSig)
    {
        const QString target = dir.path() + QStringLiteral("/OrionActivate.exe");
        writeFile(target, targetBytes);
        const QByteArray manifest = buildManifest(
            QStringLiteral("OrionActivate.exe"), sha256Hex(hashedBytes), hashedBytes.size());
        writeFile(dir.path() + QStringLiteral("/release_manifest.json"), manifest);
        if (writeSig) {
            const QByteArray sig = blankSig ? QByteArray() : ed25519Sign(seed_, manifest).toHex();
            writeFile(dir.path() + QStringLiteral("/release_manifest.sig"), sig);
        }
        return genuineBrokerInstallPresent(
            target, dir.path(), /*productionBuild=*/false,
            QStringLiteral("orion-ed25519-v1"), pub_.toBase64(), nullptr);
    }

private slots:
    void initTestCase()
    {
        QVERIFY2(ed25519Available(),
                 "libcrypto with Ed25519 must be loadable for the trust-verdict tests");
        seed_ = QByteArray(32, '\x33');
        pub_ = ed25519DerivePublicKey(seed_);
        QCOMPARE(pub_.size(), 32);
    }

    // The core enforcement property: a false verdict blocks EVERY entry mode.
    void falseVerdictBlocksAllModes()
    {
        for (const BrokerInvocation mode : kAllModes) {
            assertFullyBlocked(planBrokerActions(mode, /*selfTrusted=*/false));
        }
    }

    // Each real cause of a false verdict from the shared trust TU blocks all modes.
    void trustTuFalseCausesBlockAllModes()
    {
        // (a) tampered self: valid signature over a manifest whose hash does NOT
        //     match the bytes actually on disk.
        {
            QTemporaryDir dir;
            QVERIFY(dir.isValid());
            const bool verdict = genuineVerdictFor(
                dir, /*targetBytes=*/"TAMPERED-DIFFERENT-BYTES",
                /*hashedBytes=*/"genuine-broker-bytes", /*writeSig=*/true, /*blankSig=*/false);
            QVERIFY2(!verdict, "tampered self must not be genuine");
            for (const BrokerInvocation mode : kAllModes) {
                assertFullyBlocked(planBrokerActions(mode, verdict));
            }
        }
        // (b) missing signature file.
        {
            QTemporaryDir dir;
            QVERIFY(dir.isValid());
            const QByteArray bytes = "broker-bytes-missing-sig";
            const bool verdict = genuineVerdictFor(
                dir, bytes, bytes, /*writeSig=*/false, /*blankSig=*/false);
            QVERIFY2(!verdict, "missing signature must not be genuine");
            for (const BrokerInvocation mode : kAllModes) {
                assertFullyBlocked(planBrokerActions(mode, verdict));
            }
        }
        // (c) blank signature file.
        {
            QTemporaryDir dir;
            QVERIFY(dir.isValid());
            const QByteArray bytes = "broker-bytes-blank-sig";
            const bool verdict = genuineVerdictFor(
                dir, bytes, bytes, /*writeSig=*/true, /*blankSig=*/true);
            QVERIFY2(!verdict, "blank signature must not be genuine");
            for (const BrokerInvocation mode : kAllModes) {
                assertFullyBlocked(planBrokerActions(mode, verdict));
            }
        }
    }

    // A true verdict (correctly signed ephemeral manifest covering the real bytes)
    // permits exactly the invocation's action on each mode, exit 0.
    void trueVerdictPermitsPerModeAction()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QByteArray bytes = "MZ-genuine-broker-bytes-0123456789";
        const bool verdict = genuineVerdictFor(dir, bytes, bytes, /*writeSig=*/true,
                                               /*blankSig=*/false);
        QVERIFY2(verdict, "a correctly signed manifest must be genuine");

        const BrokerActionPlan reg = planBrokerActions(BrokerInvocation::Register, verdict);
        QVERIFY(!reg.blocked);
        QVERIFY(reg.registerHandler);
        QVERIFY(!reg.runActivation);
        QVERIFY(!reg.launchOrPrompt);
        QCOMPARE(reg.exitCode, 0);

        const BrokerActionPlan uri = planBrokerActions(BrokerInvocation::UriActivation, verdict);
        QVERIFY(!uri.blocked);
        QVERIFY(uri.registerHandler);
        QVERIFY(uri.runActivation);
        QVERIFY(!uri.launchOrPrompt);
        QCOMPARE(uri.exitCode, 0);

        const BrokerActionPlan launch = planBrokerActions(BrokerInvocation::SessionLaunch, verdict);
        QVERIFY(!launch.blocked);
        QVERIFY(launch.registerHandler);
        QVERIFY(!launch.runActivation);
        QVERIFY(launch.launchOrPrompt);
        QCOMPARE(launch.exitCode, 0);
    }
};

QTEST_GUILESS_MAIN(BrokerSelfTrustGateTests)
#include "BrokerSelfTrustGateTests.moc"
