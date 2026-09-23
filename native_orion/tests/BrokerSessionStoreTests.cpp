// [SERVER-SHARD blocker #5, Codex finding #3/#4] DPAPI session store behavior for
// the OrionActivate broker, exercised against a TEMP path (never the real user
// vault):
//   1. writeProtectedFileAtomic writes an owner-only-DACL file, leaves NO .tmp,
//      and the blob round-trips through DPAPI back to the exact plaintext.
//   2. Atomic replace: a second write swaps content and leaves no .tmp.
//   3. removeSessionFile rolls back (deletes file + .tmp).
//   4. Fail closed on empty plaintext / empty path.

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <wincrypt.h>

#include <string>

#include <QtCore/QFile>
#include <QtCore/QTemporaryDir>
#include <QtTest/QtTest>

#include "SessionStore.h"

#pragma comment(lib, "crypt32.lib")

using namespace orion::broker;

namespace {
std::wstring toW(const QString& s) { return s.toStdWString(); }

// Read a DPAPI file and unprotect it (CurrentUser, NULL entropy) exactly like
// shard_bootstrap.c does. Returns the plaintext or "" on failure.
std::string unprotectFile(const std::wstring& path)
{
    QFile f(QString::fromStdWString(path));
    if (!f.open(QIODevice::ReadOnly)) return {};
    const QByteArray enc = f.readAll();
    f.close();
    if (enc.isEmpty()) return {};

    DATA_BLOB in{}, out{};
    in.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(enc.constData()));
    in.cbData = static_cast<DWORD>(enc.size());
    if (!CryptUnprotectData(&in, nullptr, nullptr, nullptr, nullptr,
                            CRYPTPROTECT_UI_FORBIDDEN, &out)) {
        return {};
    }
    std::string plain(reinterpret_cast<char*>(out.pbData), out.cbData);
    SecureZeroMemory(out.pbData, out.cbData);
    LocalFree(out.pbData);
    return plain;
}

bool fileExists(const std::wstring& p)
{
    const DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}
} // namespace

class BrokerSessionStoreTests : public QObject
{
    Q_OBJECT

private slots:
    void writeIsAtomicOwnerOnlyAndRoundTrips()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const std::wstring finalPath = toW(dir.path() + "/shard_session.dat");
        const std::wstring tmpPath = finalPath + L".tmp";

        const std::string plaintext =
            "{\"token\":\"abc-_123\",\"token_id\":\"tid-1\",\"machine_id\":\"" +
            std::string(64, 'a') + "\"}";

        std::string err;
        QVERIFY2(writeProtectedFileAtomic(finalPath, plaintext, true, &err),
                 err.c_str());
        QVERIFY(fileExists(finalPath));
        QVERIFY2(!fileExists(tmpPath), "atomic write must not leave a .tmp behind");

        // Blob round-trips through DPAPI back to the exact plaintext.
        QCOMPARE(QString::fromStdString(unprotectFile(finalPath)),
                 QString::fromStdString(plaintext));

        // Owner-only DACL was applied.
        std::string detail;
        QVERIFY2(pathDaclIsOwnerOnly(finalPath, &detail), detail.c_str());
    }

    void secondWriteReplacesAtomically()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const std::wstring finalPath = toW(dir.path() + "/shard_session.dat");
        const std::wstring tmpPath = finalPath + L".tmp";

        QVERIFY(writeProtectedFileAtomic(finalPath, "{\"v\":\"first\"}", true, nullptr));
        QVERIFY(writeProtectedFileAtomic(finalPath, "{\"v\":\"second\"}", true, nullptr));
        QVERIFY(fileExists(finalPath));
        QVERIFY(!fileExists(tmpPath));
        QCOMPARE(QString::fromStdString(unprotectFile(finalPath)),
                 QString::fromLatin1("{\"v\":\"second\"}"));
    }

    void removeRollsBackFileAndTmp()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const std::wstring finalPath = toW(dir.path() + "/shard_session.dat");
        const std::wstring tmpPath = finalPath + L".tmp";

        QVERIFY(writeProtectedFileAtomic(finalPath, "{\"v\":\"x\"}", true, nullptr));
        QVERIFY(fileExists(finalPath));

        // Simulate a leftover temp too; rollback must clear both.
        { QFile t(QString::fromStdWString(tmpPath)); t.open(QIODevice::WriteOnly); t.write("junk"); t.close(); }

        QVERIFY(removeSessionFile(finalPath));
        QVERIFY(!fileExists(finalPath));
        QVERIFY(!fileExists(tmpPath));

        // Removing an already-absent session is success (idempotent).
        QVERIFY(removeSessionFile(finalPath));
    }

    void failsClosedOnBadArgs()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const std::wstring finalPath = toW(dir.path() + "/shard_session.dat");
        std::string err;
        // Empty plaintext -> no file written.
        QVERIFY(!writeProtectedFileAtomic(finalPath, "", true, &err));
        QVERIFY(!fileExists(finalPath));
        QVERIFY(!fileExists(finalPath + L".tmp"));
        // Empty path -> refuse.
        QVERIFY(!writeProtectedFileAtomic(L"", "{\"v\":\"x\"}", true, nullptr));
    }

    // Finding #3 (round 2): if the owner-only security descriptor cannot be built,
    // ensureVaultDir()/writeProtectedFileAtomic() must FAIL CLOSED — no vault dir,
    // no session, never a fallback to the inherited/default DACL.
    void failsClosedWhenOwnerDaclCannotBeBuilt()
    {
#ifdef ORION_PRODUCTION_BUILD
        QSKIP("ORION_BROKER_TEST_FORCE_SD_FAIL is compiled out in production builds "
              "(SessionStore.cpp guards it under #ifndef ORION_PRODUCTION_BUILD), so the "
              "owner-SD build failure cannot be forced here. The production code still "
              "fails closed on a genuine SD failure; that path is exercised in the "
              "development build where the fault-injection hook exists.");
#endif
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const std::wstring finalPath = toW(dir.path() + "/shard_session.dat");

        qputenv("ORION_BROKER_TEST_FORCE_SD_FAIL", "1");

        std::string err;
        QVERIFY2(!writeProtectedFileAtomic(finalPath, "{\"v\":\"x\"}", true, &err),
                 "write must refuse when the owner-only SD cannot be built");
        QCOMPARE(QString::fromStdString(err), QString::fromLatin1("sd_build_failed"));
        QVERIFY(!fileExists(finalPath));
        QVERIFY(!fileExists(finalPath + L".tmp"));

        // ensureVaultDir() also refuses (and creates nothing) when the SD fails.
        QVERIFY2(!ensureVaultDir(),
                 "ensureVaultDir must fail closed when the owner-only SD cannot be built");

        qunsetenv("ORION_BROKER_TEST_FORCE_SD_FAIL");

        // Sanity: with the fault cleared, a normal owner-only write succeeds again.
        QVERIFY(writeProtectedFileAtomic(finalPath, "{\"v\":\"ok\"}", true, nullptr));
        QVERIFY(fileExists(finalPath));
    }

    // Finding (round 2): rollback must REPORT failure when the session cannot be
    // deleted, so a failed launch never silently leaves a session behind.
    void rollbackReportsFailureWhenSessionUndeletable()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const std::wstring finalPath = toW(dir.path() + "/shard_session.dat");

        QVERIFY(writeProtectedFileAtomic(finalPath, "{\"v\":\"x\"}", true, nullptr));
        QVERIFY(fileExists(finalPath));

        // Read-only makes DeleteFileW fail with ERROR_ACCESS_DENIED, so the file
        // remains and removeSessionFile must return false (failure reported).
        QVERIFY(SetFileAttributesW(finalPath.c_str(), FILE_ATTRIBUTE_READONLY));
        QVERIFY2(!removeSessionFile(finalPath),
                 "rollback must report failure when the session file cannot be deleted");
        QVERIFY(fileExists(finalPath));

        // Cleanup: clear read-only; a normal rollback now succeeds and clears it.
        QVERIFY(SetFileAttributesW(finalPath.c_str(), FILE_ATTRIBUTE_NORMAL));
        QVERIFY(removeSessionFile(finalPath));
        QVERIFY(!fileExists(finalPath));
    }
};

QTEST_GUILESS_MAIN(BrokerSessionStoreTests)
#include "BrokerSessionStoreTests.moc"
