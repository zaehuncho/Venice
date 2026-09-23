// [RT-MED-09 2026-09-23] Crash-safe settings save + sign (SecurityManager transaction).
//
// A crash or forced reboot between the settings.json write and the settings.json.sig write
// used to leave NEW settings next to the OLD signature and lock the customer out. These tests
// "crash" at every point of the transaction and require the exact old pair or the exact new
// pair afterwards, never a signed copy of content the transaction did not write itself.
//
// Production policy is forced with ORION_REQUIRE_RELEASE_MANIFEST=1 (dev test builds resolve
// orionDataDir(root) to root, so every file lives in the temporary directory).

#include "SecurityManager.h"

#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QTemporaryDir>
#include <QtTest/QtTest>

using orion::SecurityManager;

namespace {

void writeFile(const QString& path, const QByteArray& bytes)
{
    QFile f(path);
    QVERIFY(f.open(QIODevice::WriteOnly | QIODevice::Truncate));
    QCOMPARE(f.write(bytes), bytes.size());
}

QByteArray readFile(const QString& path)
{
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly)) return {};
    return f.readAll();
}

const QByteArray kOld = QByteArrayLiteral("{\"shot_lead_ms\": 250, \"v\": \"old\"}\n");
const QByteArray kNew = QByteArrayLiteral("{\"shot_lead_ms\": 275, \"v\": \"new\"}\n");
const QByteArray kTampered = QByteArrayLiteral("{\"shot_lead_ms\": 999, \"v\": \"tampered\"}\n");

} // namespace

class SettingsSignatureTransactionTests final : public QObject {
    Q_OBJECT

private:
    QTemporaryDir dir_;
    QString settings() const { return dir_.path() + QStringLiteral("/settings.json"); }
    QString sig() const { return dir_.path() + QStringLiteral("/settings.json.sig"); }
    QString journal() const { return dir_.path() + QStringLiteral("/settings.json.txn"); }
    QString marker() const { return dir_.path() + QStringLiteral("/settings.signed-once"); }

    // A profile that already holds a verified OLD pair and the signed-once marker.
    void seedSignedOldPair()
    {
        writeFile(settings(), kOld);
        SecurityManager sm(dir_.path());
        QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Bootstrap);
        QVERIFY(sm.commitSettingsWrite());
        QVERIFY(sm.verifySettingsSignature());
        QVERIFY(QFile::exists(marker()));
    }

private slots:
    void initTestCase()
    {
#ifdef ORION_PRODUCTION_BUILD
        // orionDataDir() is the real per-user AppLocalData directory in a production build;
        // never let a test write settings files there.
        QSKIP("settings transaction tests run in the dev-configured test build only");
#endif
    }
    void init()
    {
        qputenv("ORION_REQUIRE_RELEASE_MANIFEST", "1");
        QVERIFY(dir_.isValid());
        QDir d(dir_.path());
        for (const QString& f : d.entryList(QDir::Files | QDir::Hidden)) d.remove(f);
    }
    void cleanup() { qunsetenv("ORION_REQUIRE_RELEASE_MANIFEST"); }

    void crashBetweenSettingsAndSignatureRestoresTheOldPair()
    {
        seedSignedOldPair();
        {
            SecurityManager sm(dir_.path());
            QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Signed);
            QVERIFY(QFile::exists(journal()));
            writeFile(settings(), kNew);   // AppConfig::save() landed ...
        }                                  // ... and the process died before the signature.
        SecurityManager next(dir_.path());
        QVERIFY(!next.verifySettingsSignature());
        QVERIFY(next.recoverInterruptedSettingsWrite()
                == SecurityManager::SettingsWriteRecovery::RolledBack);
        QCOMPARE(readFile(settings()), kOld);
        QVERIFY(next.verifySettingsSignature());
        QVERIFY(!QFile::exists(journal()));
    }

    void crashAfterTheSignatureKeepsTheNewPair()
    {
        seedSignedOldPair();
        {
            SecurityManager sm(dir_.path());
            QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Signed);
            writeFile(settings(), kNew);
            QVERIFY(sm.writeSettingsSignature());   // signed, then died before clearing the journal
        }
        SecurityManager next(dir_.path());
        QVERIFY(next.recoverInterruptedSettingsWrite()
                == SecurityManager::SettingsWriteRecovery::CompletedEarlier);
        QCOMPARE(readFile(settings()), kNew);
        QVERIFY(next.verifySettingsSignature());
        QVERIFY(!QFile::exists(journal()));
    }

    void crashBeforeTheSettingsWriteKeepsTheOldPair()
    {
        seedSignedOldPair();
        {
            SecurityManager sm(dir_.path());
            QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Signed);
        }
        SecurityManager next(dir_.path());
        QVERIFY(next.recoverInterruptedSettingsWrite()
                == SecurityManager::SettingsWriteRecovery::CompletedEarlier);
        QCOMPARE(readFile(settings()), kOld);
        QVERIFY(next.verifySettingsSignature());
    }

    void completedTransactionLeavesNoJournal()
    {
        seedSignedOldPair();
        SecurityManager sm(dir_.path());
        QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Signed);
        writeFile(settings(), kNew);
        QVERIFY(sm.commitSettingsWrite());
        QVERIFY(sm.verifySettingsSignature());
        QVERIFY(!QFile::exists(journal()));
        QVERIFY(sm.recoverInterruptedSettingsWrite()
                == SecurityManager::SettingsWriteRecovery::NoJournal);
    }

    void tamperedSettingsAreRefusedNeverResigned()
    {
        seedSignedOldPair();
        writeFile(settings(), kTampered);   // hand edit, no journal
        SecurityManager sm(dir_.path());
        QVERIFY(sm.recoverInterruptedSettingsWrite()
                == SecurityManager::SettingsWriteRecovery::NoJournal);
        QVERIFY(!sm.verifySettingsSignature());
        QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Refused);
        QVERIFY(!sm.verifySettingsSignature());
        QCOMPARE(readFile(settings()), kTampered);
    }

    void deletingTheSignatureAfterFirstSignIsNotABootstrap()
    {
        seedSignedOldPair();
        writeFile(settings(), kTampered);
        QVERIFY(QFile::remove(sig()));
        SecurityManager sm(dir_.path());
        QVERIFY(!sm.settingsBootstrapAllowed());
        QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Refused);
        QVERIFY(!QFile::exists(sig()));
    }

    void freshProfileBootstrapsExactlyOnce()
    {
        SecurityManager sm(dir_.path());
        QVERIFY(sm.settingsBootstrapAllowed());
        writeFile(settings(), kOld);
        QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Bootstrap);
        QVERIFY(sm.commitSettingsWrite());
        QVERIFY(sm.verifySettingsSignature());
        QVERIFY(!sm.settingsBootstrapAllowed());
    }

    void forgedJournalWithAnUnverifiedSnapshotSignsNothing()
    {
        seedSignedOldPair();
        writeFile(settings(), kTampered);
        writeFile(dir_.path() + QStringLiteral("/settings.json.prev"), kTampered);
        writeFile(dir_.path() + QStringLiteral("/settings.json.prev.sig"), QByteArrayLiteral("00ff\n"));
        writeFile(journal(), QByteArrayLiteral("00ff\n"));
        SecurityManager sm(dir_.path());
        QVERIFY(sm.recoverInterruptedSettingsWrite()
                == SecurityManager::SettingsWriteRecovery::Unrecoverable);
        QVERIFY(!sm.verifySettingsSignature());
        QCOMPARE(readFile(settings()), kTampered);
        QVERIFY(!QFile::exists(journal()));
    }

    void devPolicyKeepsTheLegacyResignAndWritesNoJournal()
    {
        qunsetenv("ORION_REQUIRE_RELEASE_MANIFEST");
        writeFile(settings(), kOld);
        SecurityManager sm(dir_.path());
        QVERIFY(sm.beginSettingsWrite() == SecurityManager::SettingsWriteGate::Bootstrap);
        QVERIFY(sm.commitSettingsWrite());
        QVERIFY(sm.verifySettingsSignature());
        QVERIFY(!QFile::exists(journal()));
        QVERIFY(!QFile::exists(marker()));
    }
};

QTEST_GUILESS_MAIN(SettingsSignatureTransactionTests)
#include "SettingsSignatureTransactionTests.moc"
