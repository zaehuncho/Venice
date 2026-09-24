// [P-H 2026-09-23] Remembered sign-in (SecurityManager store + LicenseClient refusal
// classification + LicenseHeartbeatPolicy retry decision).
//
// The production build never remembered a sign-in: every launch showed AuthGate and a
// keyless (PAIR-) customer needed a new one-time code each time. The canonical key is now
// kept DPAPI-protected per user/PC and re-submitted to the server at launch. These tests
// pin the storage contract and the keep/clear policy; they never talk to a server.
//
// File tests run in the dev-configured test build only: there orionDataDir(root) == root,
// so every file lives in a QTemporaryDir. In a production build orionDataDir() is the real
// per-user AppLocalData directory and the file tests skip (the pure policy tests still run).

#include "LicenseClient.h"
#include "LicenseHeartbeatPolicy.h"
#include "SecurityManager.h"

#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QStandardPaths>
#include <QtCore/QStringList>
#include <QtCore/QTemporaryDir>
#include <QtTest/QtTest>

#ifdef Q_OS_WIN
#include <Windows.h>
#include <wincrypt.h>
#pragma comment(lib, "crypt32.lib")
#endif

using orion::LicenseHeartbeatBackoff;
using orion::SecurityManager;

namespace {

// Test-only key shapes. Not real licences (the backend alphabet has no such rows).
const QString kKeyA = QStringLiteral("TEST-AAAA-BBBB-C0DE");
const QString kKeyB = QStringLiteral("TEST-DDDD-EEEE-F1X9");
const QString kPair = QStringLiteral("PAIR-") + QString(32, QLatin1Char('A'));

QStringList g_messages;
QtMessageHandler g_previousHandler = nullptr;

void captureMessages(QtMsgType type, const QMessageLogContext& ctx, const QString& msg)
{
    g_messages.append(msg);
    if (g_previousHandler) g_previousHandler(type, ctx, msg);
}

void writeFile(const QString& path, const QByteArray& bytes)
{
    QDir().mkpath(QFileInfo(path).absolutePath());
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

// Builds a correctly DPAPI-protected record with an arbitrary payload, so binding and
// payload checks can be exercised past the decrypt step.
QByteArray protectedRecord(const QJsonObject& payload, const QByteArray& entropy)
{
#ifndef Q_OS_WIN
    Q_UNUSED(payload);
    Q_UNUSED(entropy);
    return {};
#else
    const QByteArray plain = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    DATA_BLOB in {};
    in.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(plain.constData()));
    in.cbData = static_cast<DWORD>(plain.size());
    DATA_BLOB ent {};
    ent.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(entropy.constData()));
    ent.cbData = static_cast<DWORD>(entropy.size());
    DATA_BLOB out {};
    if (!CryptProtectData(&in, L"test", &ent, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out)) {
        return {};
    }
    const QByteArray blob(reinterpret_cast<const char*>(out.pbData), static_cast<int>(out.cbData));
    LocalFree(out.pbData);
    QJsonObject outer;
    outer.insert(QStringLiteral("schema"), QStringLiteral("venice.remembered_signin_file.v1"));
    outer.insert(QStringLiteral("blob_b64"), QString::fromLatin1(blob.toBase64()));
    return QJsonDocument(outer).toJson();
#endif
}

// QSKIP returns from the CURRENT function, so the guard must be a macro expanded inside
// each test slot (a helper function would skip only itself and let the test run on).
#if defined(ORION_PRODUCTION_BUILD)
#define REQUIRE_DEV_FILE_BUILD()     QSKIP("remembered sign-in FILE tests run in the dev-configured test build only")
#elif !defined(Q_OS_WIN)
#define REQUIRE_DEV_FILE_BUILD()     QSKIP("DPAPI is Windows-only; without it nothing is remembered by design")
#else
#define REQUIRE_DEV_FILE_BUILD() do {} while (false)
#endif

} // namespace

class RememberedSignInTests final : public QObject {
    Q_OBJECT

private:
    QTemporaryDir dir_;
    QString storePath() const { return dir_.path() + QStringLiteral("/.vault/venice_signin.dat"); }

    void assertNoKeyText(const QStringList& lines)
    {
        for (const QString& line : lines) {
            QVERIFY2(!line.contains(kKeyA) && !line.contains(kKeyB) && !line.contains(kPair),
                     qPrintable(QStringLiteral("key text leaked: ") + line.left(12)));
        }
    }

private slots:
    void initTestCase()
    {
        QStandardPaths::setTestModeEnabled(true);
        QVERIFY(dir_.isValid());
    }
    void init()
    {
        QDir(dir_.path() + QStringLiteral("/.vault")).removeRecursively();
        g_messages.clear();
        g_previousHandler = qInstallMessageHandler(captureMessages);
    }
    void cleanup()
    {
        qInstallMessageHandler(g_previousHandler);
        g_previousHandler = nullptr;
    }

    // ---- key shape --------------------------------------------------------------------

    void onlyCanonicalKeysAreRememberable()
    {
        QVERIFY(orion::isRememberableLicenseKey(kKeyA));
        QVERIFY(orion::isRememberableLicenseKey(QStringLiteral("AB12-CD34-EF56-GH78")));
        QVERIFY(!orion::isRememberableLicenseKey(kPair));
        QVERIFY(!orion::isRememberableLicenseKey(QStringLiteral("PAIR-AAAA-BBBB-CCCC")));
        QVERIFY(!orion::isRememberableLicenseKey(QStringLiteral("NVDEV-AAAA-BBBB-CCCC")));
        QVERIFY(!orion::isRememberableLicenseKey(QStringLiteral("test-aaaa-bbbb-c0de")));
        QVERIFY(!orion::isRememberableLicenseKey(QStringLiteral(" TEST-AAAA-BBBB-C0DE")));
        QVERIFY(!orion::isRememberableLicenseKey(QStringLiteral("TEST-AAAA-BBBB-C0DE\n")));
        QVERIFY(!orion::isRememberableLicenseKey(QStringLiteral("TEST-AAAA-BBBB")));
        QVERIFY(!orion::isRememberableLicenseKey(QString()));
    }

    // ---- store / load -----------------------------------------------------------------

    void storeThenLoadRoundTrips()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        QString error;
        QVERIFY2(sm.storeRememberedLicenseKey(kKeyA, &error), qPrintable(error));
        QVERIFY(QFile::exists(storePath()));
        QVERIFY(sm.hasRememberedLicenseKey());
        // Encrypted at rest: the file never holds the key text.
        const QByteArray raw = readFile(storePath());
        QVERIFY(!raw.isEmpty());
        QVERIFY(!raw.contains(kKeyA.toLatin1()));
        QVERIFY(!raw.contains("license_key"));

        SecurityManager::RememberedKeyLoad outcome = SecurityManager::RememberedKeyLoad::None;
        QString detail;
        QCOMPARE(sm.loadRememberedLicenseKey(&outcome, &detail), kKeyA);
        QVERIFY(outcome == SecurityManager::RememberedKeyLoad::Loaded);
        QVERIFY(detail.contains(kKeyA.right(4)));
        QVERIFY(!detail.contains(kKeyA));
        // A fresh manager (next launch) reads the same record.
        SecurityManager nextLaunch(dir_.path());
        QCOMPARE(nextLaunch.loadRememberedLicenseKey(), kKeyA);
    }

    void storeReplacesThePreviousKey()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        QVERIFY(sm.storeRememberedLicenseKey(kKeyA));
        QVERIFY(sm.storeRememberedLicenseKey(kKeyB));
        QCOMPARE(sm.loadRememberedLicenseKey(), kKeyB);
    }

    void pairCodesAndNonCanonicalKeysAreNeverStored()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        for (const QString& bad : {kPair, QStringLiteral("NVDEV-LOCAL-TEST-KEY1"),
                                   QStringLiteral("test-aaaa-bbbb-c0de"),
                                   QStringLiteral(" TEST-AAAA-BBBB-C0DE "), QString()}) {
            QString error;
            QVERIFY(!sm.storeRememberedLicenseKey(bad, &error));
            QVERIFY(!error.isEmpty());
            QVERIFY(!error.contains(bad.trimmed()) || bad.trimmed().isEmpty());
            QVERIFY(!QFile::exists(storePath()));
        }
        // A refused store never disturbs a good record already there.
        QVERIFY(sm.storeRememberedLicenseKey(kKeyA));
        QVERIFY(!sm.storeRememberedLicenseKey(kPair));
        QCOMPARE(sm.loadRememberedLicenseKey(), kKeyA);
    }

    void missingFileIsNone()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        SecurityManager::RememberedKeyLoad outcome = SecurityManager::RememberedKeyLoad::Loaded;
        QVERIFY(sm.loadRememberedLicenseKey(&outcome).isEmpty());
        QVERIFY(outcome == SecurityManager::RememberedKeyLoad::None);
    }

    void corruptFilesAreDeletedAndNeverUnlock_data()
    {
        QTest::addColumn<QByteArray>("bytes");
        QTest::newRow("empty") << QByteArray();
        QTest::newRow("garbage") << QByteArray("\x01\x02not json at all");
        QTest::newRow("json-array") << QByteArray("[1,2,3]");
        QTest::newRow("wrong-schema") << QByteArray(
            "{\"schema\":\"orion.local_entitlement_cache.v1\",\"blob_b64\":\"AAAA\"}");
        QTest::newRow("empty-blob") << QByteArray(
            "{\"schema\":\"venice.remembered_signin_file.v1\",\"blob_b64\":\"\"}");
        QTest::newRow("undecryptable-blob") << QByteArray(
            "{\"schema\":\"venice.remembered_signin_file.v1\",\"blob_b64\":\"AAECAwQFBgcICQ==\"}");
        QTest::newRow("plaintext-key") << (QByteArray(
            "{\"schema\":\"venice.remembered_signin_file.v1\",\"license_key\":\"")
            + kKeyA.toLatin1() + "\"}");
        QTest::newRow("oversized") << QByteArray(70 * 1024, 'x');
    }
    void corruptFilesAreDeletedAndNeverUnlock()
    {
        REQUIRE_DEV_FILE_BUILD();
        QFETCH(QByteArray, bytes);
        writeFile(storePath(), bytes);
        QVERIFY(QFile::exists(storePath()));
        SecurityManager sm(dir_.path());
        SecurityManager::RememberedKeyLoad outcome = SecurityManager::RememberedKeyLoad::None;
        QString detail;
        QVERIFY(sm.loadRememberedLicenseKey(&outcome, &detail).isEmpty());
        QVERIFY(outcome == SecurityManager::RememberedKeyLoad::Cleared);
        QVERIFY(!QFile::exists(storePath()));
        QVERIFY(!detail.contains(kKeyA));
    }

    void tamperedCiphertextIsDeleted()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        QVERIFY(sm.storeRememberedLicenseKey(kKeyA));
        QJsonObject outer = QJsonDocument::fromJson(readFile(storePath())).object();
        QByteArray blob = QByteArray::fromBase64(outer.value(QStringLiteral("blob_b64")).toString().toLatin1());
        QVERIFY(blob.size() > 40);
        blob[blob.size() - 5] = static_cast<char>(blob[blob.size() - 5] ^ 0x5A);
        outer.insert(QStringLiteral("blob_b64"), QString::fromLatin1(blob.toBase64()));
        writeFile(storePath(), QJsonDocument(outer).toJson());
        SecurityManager::RememberedKeyLoad outcome = SecurityManager::RememberedKeyLoad::None;
        QVERIFY(sm.loadRememberedLicenseKey(&outcome).isEmpty());
        QVERIFY(outcome == SecurityManager::RememberedKeyLoad::Cleared);
        QVERIFY(!QFile::exists(storePath()));
    }

    void wrongEntropyIsDeleted()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        QJsonObject binding;
        binding.insert(QStringLiteral("machine_id"), sm.machineId());
        QJsonObject payload;
        payload.insert(QStringLiteral("schema"), QStringLiteral("venice.remembered_signin.v1"));
        payload.insert(QStringLiteral("license_key"), kKeyA);
        payload.insert(QStringLiteral("binding"), binding);
        // The entitlement cache's entropy must not open a sign-in record.
        writeFile(storePath(), protectedRecord(
            payload, QByteArrayLiteral("orion-entitlement-v1|") + sm.machineId().toUtf8()));
        SecurityManager::RememberedKeyLoad outcome = SecurityManager::RememberedKeyLoad::None;
        QVERIFY(sm.loadRememberedLicenseKey(&outcome).isEmpty());
        QVERIFY(outcome == SecurityManager::RememberedKeyLoad::Cleared);
        QVERIFY(!QFile::exists(storePath()));
    }

    void foreignBindingOrNonCanonicalPayloadIsDeleted_data()
    {
        QTest::addColumn<QString>("machineId");
        QTest::addColumn<bool>("sameUser");
        QTest::addColumn<QString>("key");
        QTest::newRow("other-pc") << QStringLiteral("some-other-machine-id") << true << kKeyA;
        QTest::newRow("other-windows-user") << QString() << false << kKeyA;
        QTest::newRow("pair-code-inside") << QString() << true << kPair;
        QTest::newRow("lowercase-inside") << QString() << true << kKeyA.toLower();
    }
    void foreignBindingOrNonCanonicalPayloadIsDeleted()
    {
        REQUIRE_DEV_FILE_BUILD();
        QFETCH(QString, machineId);
        QFETCH(bool, sameUser);
        QFETCH(QString, key);
        SecurityManager sm(dir_.path());
        // The record is correctly DPAPI-protected with the sign-in entropy, so these rows
        // exercise the checks AFTER decryption: binding (machine id, Windows user) and key
        // shape. "Same user" uses %USERNAME%; should it ever differ from GetUserNameW the
        // row is still (correctly) rejected, so every row must end Cleared.
        QJsonObject binding;
        binding.insert(QStringLiteral("machine_id"), machineId.isEmpty() ? sm.machineId() : machineId);
        binding.insert(QStringLiteral("user"), sameUser ? qEnvironmentVariable("USERNAME")
                                                        : QStringLiteral("someone-else"));
        QJsonObject payload;
        payload.insert(QStringLiteral("schema"), QStringLiteral("venice.remembered_signin.v1"));
        payload.insert(QStringLiteral("license_key"), key);
        payload.insert(QStringLiteral("binding"), binding);
        writeFile(storePath(), protectedRecord(
            payload, QByteArrayLiteral("venice-remembered-signin-v1|") + sm.machineId().toUtf8()));
        SecurityManager::RememberedKeyLoad outcome = SecurityManager::RememberedKeyLoad::None;
        const QString loaded = sm.loadRememberedLicenseKey(&outcome);
        QVERIFY(loaded.isEmpty());
        QVERIFY(outcome == SecurityManager::RememberedKeyLoad::Cleared);
        QVERIFY(!QFile::exists(storePath()));
    }

    // ---- sign-out -----------------------------------------------------------------------

    void signOutClearsTheStore()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        QVERIFY(sm.storeRememberedLicenseKey(kKeyA));
        QString error;
        QVERIFY2(sm.clearRememberedLicenseKey(&error), qPrintable(error));
        QVERIFY(!QFile::exists(storePath()));
        QVERIFY(!sm.hasRememberedLicenseKey());
        SecurityManager::RememberedKeyLoad outcome = SecurityManager::RememberedKeyLoad::Loaded;
        QVERIFY(sm.loadRememberedLicenseKey(&outcome).isEmpty());
        QVERIFY(outcome == SecurityManager::RememberedKeyLoad::None);
        QVERIFY(sm.clearRememberedLicenseKey());   // idempotent
    }

    // ---- server verdict policy ------------------------------------------------------------

    void definitiveRefusalsClearTheStoredKey_data()
    {
        QTest::addColumn<QString>("code");
        for (const char* code : {"invalid_key", "license_revoked", "revoked", "license_expired",
                                 "expired", "license_invalid_status", "inactive", "frozen",
                                 "blacklisted", "device_mismatch", "device_limit_reached",
                                 "subscription_required", "discord_signin_required",
                                 "trial_used", "pair_invalid"}) {
            QTest::newRow(code) << QString::fromLatin1(code);
        }
    }
    void definitiveRefusalsClearTheStoredKey()
    {
        QFETCH(QString, code);
        QVERIFY(orion::isRememberedSignInRefusalCode(code));
        LicenseHeartbeatBackoff backoff;
        backoff.recordFailure();   // an earlier transient failure must not matter
        const orion::RememberedSignInDecision d =
            orion::rememberedSignInAfterFailure(orion::isRememberedSignInRefusalCode(code),
                                               code == QLatin1String("rate_limited"), backoff);
        QVERIFY(d.clearStoredKey);
        QCOMPARE(d.retryDelayMs, -1);
        QCOMPARE(backoff.consecutiveFailures(), 0);
    }

    void definitiveRefusalDeletesTheFile()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        QVERIFY(sm.storeRememberedLicenseKey(kKeyA));
        LicenseHeartbeatBackoff backoff;
        const auto d = orion::rememberedSignInAfterFailure(
            orion::isRememberedSignInRefusalCode(QStringLiteral("license_revoked")), false, backoff);
        QVERIFY(d.clearStoredKey);
        // What OrionAppController::forgetRememberedSignIn() does with that decision.
        QVERIFY(sm.clearRememberedLicenseKey());
        QVERIFY(!sm.hasRememberedLicenseKey());
    }

    void transientFailuresKeepTheKeyAndRetryOnTheLadder_data()
    {
        QTest::addColumn<QString>("code");
        for (const char* code : {"", "internal_error", "entitlement_unavailable",
                                 "kill_state_unavailable", "config_unavailable",
                                 "timestamp_expired", "replay_detected", "invalid_timestamp",
                                 "license_record_invalid", "service_disabled",
                                 "service_disabled: maintenance", "version_blocked",
                                 "some_future_code", "REVOKED", "license revoked"}) {
            QTest::newRow(*code ? code : "transport") << QString::fromLatin1(code);
        }
    }
    void transientFailuresKeepTheKeyAndRetryOnTheLadder()
    {
        QFETCH(QString, code);
        QVERIFY(!orion::isRememberedSignInRefusalCode(code));
        LicenseHeartbeatBackoff backoff;
        const int expected[] = {15'000, 30'000, 60'000, LicenseHeartbeatBackoff::kNormalIntervalMs,
                                LicenseHeartbeatBackoff::kNormalIntervalMs};
        for (int want : expected) {
            const auto d = orion::rememberedSignInAfterFailure(false, false, backoff);
            QVERIFY(!d.clearStoredKey);
            QCOMPARE(d.retryDelayMs, want);
        }
    }

    void rateLimitedJumpsToTheSixtySecondRung()
    {
        QVERIFY(!orion::isRememberedSignInRefusalCode(QStringLiteral("rate_limited")));
        LicenseHeartbeatBackoff backoff;
        const auto d = orion::rememberedSignInAfterFailure(false, true, backoff);
        QVERIFY(!d.clearStoredKey);
        QCOMPARE(d.retryDelayMs, 60'000);
    }

    void transientFailureLeavesTheFileInPlace()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        QVERIFY(sm.storeRememberedLicenseKey(kKeyA));
        LicenseHeartbeatBackoff backoff;
        const auto d = orion::rememberedSignInAfterFailure(
            orion::isRememberedSignInRefusalCode(QString()), false, backoff);
        QVERIFY(!d.clearStoredKey);
        QCOMPARE(sm.loadRememberedLicenseKey(), kKeyA);
    }

    void keyLevelKillCodesAreAlsoRememberedRefusals()
    {
        // Every heartbeat kill code that is a verdict on the KEY also deletes the store;
        // the two owner-wide gates (pause, version) keep it.
        for (const char* code : {"revoked", "expired", "device_mismatch", "invalid_key",
                                 "inactive", "frozen", "blacklisted", "subscription_required"}) {
            QVERIFY2(orion::isLicenseKillCode(QString::fromLatin1(code)), code);
            QVERIFY2(orion::isRememberedSignInRefusalCode(QString::fromLatin1(code)), code);
        }
        for (const char* code : {"service_disabled", "version_blocked"}) {
            QVERIFY2(orion::isLicenseKillCode(QString::fromLatin1(code)), code);
            QVERIFY2(!orion::isRememberedSignInRefusalCode(QString::fromLatin1(code)), code);
        }
    }

    // ---- no key text anywhere ---------------------------------------------------------------

    void noKeyTextInLogsOrSecurityEvents()
    {
        REQUIRE_DEV_FILE_BUILD();
        SecurityManager sm(dir_.path());
        QStringList events;
        connect(&sm, &SecurityManager::securityEvent, this,
                [&events](const QString& e, const QString& d) { events << e << d; });
        QString error;
        QString detail;
        SecurityManager::RememberedKeyLoad outcome{};
        QVERIFY(sm.storeRememberedLicenseKey(kKeyA, &error));
        (void)sm.loadRememberedLicenseKey(&outcome, &detail);
        QStringList details{error, detail};
        QVERIFY(!sm.storeRememberedLicenseKey(kPair, &error));
        details << error;
        writeFile(storePath(), QByteArray("{\"schema\":\"venice.remembered_signin_file.v1\",\"blob_b64\":\"AAAA\"}"));
        (void)sm.loadRememberedLicenseKey(&outcome, &detail);
        details << detail;
        QVERIFY(sm.clearRememberedLicenseKey(&error));
        details << error;
        assertNoKeyText(details);
        assertNoKeyText(events);
        assertNoKeyText(g_messages);
    }
};

QTEST_GUILESS_MAIN(RememberedSignInTests)
#include "RememberedSignInTests.moc"
