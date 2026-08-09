#include "Ed25519.h"

#include <QtCore/QByteArrayView>
#include <QtCore/QCoreApplication>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QLibrary>
#include <QtCore/QString>

#include <cstddef>

#ifdef Q_OS_WIN
#include <Windows.h>
#endif

namespace orion {

namespace {

// NID_ED25519 from OpenSSL's obj_mac.h. Stable across OpenSSL 1.1.1/3.x.
constexpr int kEvpPkeyEd25519 = 1087;

struct LibCrypto {
    bool verifyReady = false;
    bool signReady = false;

    void* (*newRawPublicKey)(int, void*, const unsigned char*, size_t) = nullptr;
    void* (*newRawPrivateKey)(int, void*, const unsigned char*, size_t) = nullptr;
    int   (*getRawPublicKey)(const void*, unsigned char*, size_t*) = nullptr;
    void* (*mdCtxNew)() = nullptr;
    void  (*mdCtxFree)(void*) = nullptr;
    void  (*pkeyFree)(void*) = nullptr;
    int   (*digestVerifyInit)(void*, void**, const void*, void*, void*) = nullptr;
    int   (*digestVerify)(void*, const unsigned char*, size_t, const unsigned char*, size_t) = nullptr;
    int   (*digestSignInit)(void*, void**, const void*, void*, void*) = nullptr;
    int   (*digestSign)(void*, unsigned char*, size_t*, const unsigned char*, size_t) = nullptr;
};

// RFC 8032, section 7.1, test vector 1 (empty message). Symbol presence alone
// is not enough to call a provider Ed25519-capable: Windows can expose an
// unrelated/LibreSSL-compatible libcrypto.dll whose EVP entry points resolve
// while OpenSSL's stable NID 1087 is unsupported. A known-answer test makes the
// availability bit describe working Ed25519 verification, not just ABI shape.
bool verifyKnownAnswer(const LibCrypto& c)
{
    const QByteArray publicKey = QByteArray::fromHex(
        QByteArrayLiteral("d75a980182b10ab7d54bfed3c964073a"
                          "0ee172f3daa62325af021a68f707511a"));
    const QByteArray signature = QByteArray::fromHex(
        QByteArrayLiteral("e5564300c360ac729086e2cc806e828a"
                          "84877f1eb8e5d974d873e06522490155"
                          "5fb8821590a33bacc61e39701cf9b46b"
                          "d25bf5f0595bbe24655141438e7a100b"));
    void* pkey = c.newRawPublicKey(
        kEvpPkeyEd25519, nullptr,
        reinterpret_cast<const unsigned char*>(publicKey.constData()),
        static_cast<size_t>(publicKey.size()));
    if (!pkey) {
        return false;
    }
    void* ctx = c.mdCtxNew();
    const unsigned char emptyMessage = 0;
    const bool ok = ctx
        && c.digestVerifyInit(ctx, nullptr, nullptr, nullptr, pkey) == 1
        && c.digestVerify(
               ctx,
               reinterpret_cast<const unsigned char*>(signature.constData()),
               static_cast<size_t>(signature.size()),
               &emptyMessage, 0) == 1;
    if (ctx) {
        c.mdCtxFree(ctx);
    }
    c.pkeyFree(pkey);
    return ok;
}

bool signingKnownAnswer(const LibCrypto& c)
{
    const QByteArray privateSeed = QByteArray::fromHex(
        QByteArrayLiteral("9d61b19deffd5a60ba844af492ec2cc4"
                          "4449c5697b326919703bac031cae7f60"));
    const QByteArray expectedPublic = QByteArray::fromHex(
        QByteArrayLiteral("d75a980182b10ab7d54bfed3c964073a"
                          "0ee172f3daa62325af021a68f707511a"));
    const QByteArray expectedSignature = QByteArray::fromHex(
        QByteArrayLiteral("e5564300c360ac729086e2cc806e828a"
                          "84877f1eb8e5d974d873e06522490155"
                          "5fb8821590a33bacc61e39701cf9b46b"
                          "d25bf5f0595bbe24655141438e7a100b"));
    void* pkey = c.newRawPrivateKey(
        kEvpPkeyEd25519, nullptr,
        reinterpret_cast<const unsigned char*>(privateSeed.constData()),
        static_cast<size_t>(privateSeed.size()));
    if (!pkey) {
        return false;
    }

    QByteArray publicKey(32, Qt::Uninitialized);
    size_t publicKeySize = static_cast<size_t>(publicKey.size());
    const bool publicOk = c.getRawPublicKey(
                              pkey,
                              reinterpret_cast<unsigned char*>(publicKey.data()),
                              &publicKeySize) == 1
        && publicKeySize == static_cast<size_t>(expectedPublic.size())
        && publicKey == expectedPublic;

    void* ctx = c.mdCtxNew();
    QByteArray signature(64, Qt::Uninitialized);
    size_t signatureSize = static_cast<size_t>(signature.size());
    const unsigned char emptyMessage = 0;
    const bool signatureOk = ctx
        && c.digestSignInit(ctx, nullptr, nullptr, nullptr, pkey) == 1
        && c.digestSign(
               ctx, reinterpret_cast<unsigned char*>(signature.data()),
               &signatureSize, &emptyMessage, 0) == 1
        && signatureSize == static_cast<size_t>(expectedSignature.size())
        && signature == expectedSignature;
    if (ctx) {
        c.mdCtxFree(ctx);
    }
    c.pkeyFree(pkey);
    return publicOk && signatureOk;
}

#ifdef ORION_PRODUCTION_BUILD
bool validPinnedHash(const QByteArray& hash)
{
    if (hash.size() != 64) {
        return false;
    }
    for (const char c : hash) {
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
            return false;
        }
    }
    return true;
}

bool hashPinnedLibrary(const QString& path, QByteArray* digest
#ifdef Q_OS_WIN
                       , HANDLE* lockedHandle
#endif
)
{
    if (digest) {
        digest->clear();
    }
#ifdef Q_OS_WIN
    if (lockedHandle) {
        *lockedHandle = INVALID_HANDLE_VALUE;
    }
    const HANDLE handle = CreateFileW(
        reinterpret_cast<LPCWSTR>(path.utf16()), GENERIC_READ,
        FILE_SHARE_READ, nullptr, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        return false;
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    QByteArray buffer(64 * 1024, Qt::Uninitialized);
    for (;;) {
        DWORD received = 0;
        if (!ReadFile(handle, buffer.data(), static_cast<DWORD>(buffer.size()), &received, nullptr)) {
            CloseHandle(handle);
            return false;
        }
        if (received == 0) {
            break;
        }
        hash.addData(QByteArrayView(buffer.constData(), static_cast<qsizetype>(received)));
    }
    if (digest) {
        *digest = hash.result();
    }
    if (lockedHandle) {
        *lockedHandle = handle; // Keep write/delete sharing denied through QLibrary::load().
    } else {
        CloseHandle(handle);
    }
    return true;
#else
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return false;
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    while (!file.atEnd()) {
        hash.addData(file.read(1024 * 1024));
    }
    if (digest) {
        *digest = hash.result();
    }
    return true;
#endif
}
#endif

const LibCrypto& libcrypto()
{
    static LibCrypto lc = []() {
        LibCrypto c;
        // Leaked on purpose: keeping the QLibrary alive for the process lifetime
        // ensures the resolved function pointers remain valid (the DLL is also
        // used by Qt's TLS stack, but we must not rely on that).
        static QLibrary* lib = new QLibrary();
#ifdef ORION_PRODUCTION_BUILD
#ifndef ORION_LIBCRYPTO_SHA256
        // Production without a build-pinned crypto provider has no trustworthy
        // signature verifier. Fail closed instead of searching PATH/app state.
        qCritical("[Ed25519] No build-pinned libcrypto (ORION_LIBCRYPTO_SHA256 undefined); "
                  "signature verification is unavailable and every downstream security check "
                  "will fail closed.");
        return c;
#else
        // [5c 2026-08-08] Make each libcrypto failure LOUD. Previously any of these paths
        // returned an empty provider silently, so ed25519Available() went false and callers
        // reported only a generic "verifier unavailable" — hiding the real cause (most often
        // antivirus quarantining or altering the pinned DLL). These are qCritical so the cause
        // is diagnosable from the log; the fail-closed return value is unchanged.
        const QByteArray expectedHex = QByteArrayLiteral(ORION_LIBCRYPTO_SHA256).toLower();
        if (!validPinnedHash(expectedHex)) {
            qCritical("[Ed25519] Compiled libcrypto pin (ORION_LIBCRYPTO_SHA256) is malformed; "
                      "signature verification unavailable.");
            return c;
        }
        const QString libraryPath = QDir(QCoreApplication::applicationDirPath())
                                        .absoluteFilePath(QStringLiteral("libcrypto-3-x64.dll"));
        if (!QFileInfo(libraryPath).isFile() || !QDir::isAbsolutePath(libraryPath)) {
            qCritical("[Ed25519] Pinned crypto provider libcrypto-3-x64.dll is MISSING from the "
                      "install directory (%s). Antivirus may have quarantined it; reinstall Venice. "
                      "Signature verification is unavailable.",
                      qUtf8Printable(libraryPath));
            return c;
        }
        QByteArray actualDigest;
#ifdef Q_OS_WIN
        HANDLE lockedHandle = INVALID_HANDLE_VALUE;
        if (!hashPinnedLibrary(libraryPath, &actualDigest, &lockedHandle)
            || actualDigest.toHex() != expectedHex) {
            if (lockedHandle != INVALID_HANDLE_VALUE) {
                CloseHandle(lockedHandle);
            }
            qCritical("[Ed25519] libcrypto-3-x64.dll bytes do NOT match the build-time pin "
                      "(the file was altered — antivirus quarantine/repair or tampering). "
                      "Reinstall Venice. Signature verification is unavailable.");
            return c;
        }
#else
        if (!hashPinnedLibrary(libraryPath, &actualDigest)
            || actualDigest.toHex() != expectedHex) {
            qCritical("[Ed25519] libcrypto-3-x64.dll bytes do NOT match the build-time pin; "
                      "signature verification is unavailable.");
            return c;
        }
#endif
        lib->setFileName(libraryPath);
        const bool loaded = lib->load();
#ifdef Q_OS_WIN
        CloseHandle(lockedHandle);
#endif
        if (!loaded) {
            qCritical("[Ed25519] libcrypto-3-x64.dll matched the pin but FAILED to load (%s); "
                      "signature verification is unavailable.",
                      qUtf8Printable(lib->errorString()));
            return c;
        }
#endif
#else
        // Prefer an explicitly staged OpenSSL 3 runtime beside the executable.
        // On Windows, never fall through to PATH or generic `libcrypto.dll`:
        // either can resolve to a different or attacker-planted provider. The
        // verification script/CMake stage the configured versioned DLL beside
        // the executable. The known-answer tests below then prove that exact
        // app-local provider can perform Ed25519 before it receives authority.
#ifdef Q_OS_WIN
        const QString applicationDir = QCoreApplication::applicationDirPath();
        for (const QString& path : {
                 QDir(applicationDir).absoluteFilePath(
                     QStringLiteral("libcrypto-3-x64.dll")),
                 QDir(applicationDir).absoluteFilePath(
                     QStringLiteral("libcrypto-3.dll")),
             }) {
            if (!QFileInfo(path).isFile()) {
                continue;
            }
            lib->setFileName(path);
            if (lib->load()) {
                break;
            }
        }
#else
        for (const char* name : {"libcrypto-3", "libcrypto", "crypto"}) {
            lib->setFileName(QString::fromLatin1(name));
            if (lib->load()) {
                break;
            }
        }
#endif
#endif
        if (!lib->isLoaded()) {
            return c;
        }
        const auto R = [](QLibrary* l, const char* sym) { return l->resolve(sym); };
        c.newRawPublicKey  = reinterpret_cast<decltype(c.newRawPublicKey)>(R(lib, "EVP_PKEY_new_raw_public_key"));
        c.newRawPrivateKey = reinterpret_cast<decltype(c.newRawPrivateKey)>(R(lib, "EVP_PKEY_new_raw_private_key"));
        c.getRawPublicKey  = reinterpret_cast<decltype(c.getRawPublicKey)>(R(lib, "EVP_PKEY_get_raw_public_key"));
        c.mdCtxNew         = reinterpret_cast<decltype(c.mdCtxNew)>(R(lib, "EVP_MD_CTX_new"));
        c.mdCtxFree        = reinterpret_cast<decltype(c.mdCtxFree)>(R(lib, "EVP_MD_CTX_free"));
        c.pkeyFree         = reinterpret_cast<decltype(c.pkeyFree)>(R(lib, "EVP_PKEY_free"));
        c.digestVerifyInit = reinterpret_cast<decltype(c.digestVerifyInit)>(R(lib, "EVP_DigestVerifyInit"));
        c.digestVerify     = reinterpret_cast<decltype(c.digestVerify)>(R(lib, "EVP_DigestVerify"));
        c.digestSignInit   = reinterpret_cast<decltype(c.digestSignInit)>(R(lib, "EVP_DigestSignInit"));
        c.digestSign       = reinterpret_cast<decltype(c.digestSign)>(R(lib, "EVP_DigestSign"));

        const bool verifySymbols = c.newRawPublicKey && c.mdCtxNew && c.mdCtxFree
            && c.pkeyFree && c.digestVerifyInit && c.digestVerify;
        c.verifyReady = verifySymbols && verifyKnownAnswer(c);
        const bool signSymbols = c.newRawPrivateKey && c.getRawPublicKey
            && c.mdCtxNew && c.mdCtxFree && c.pkeyFree
            && c.digestSignInit && c.digestSign;
        c.signReady = c.verifyReady && signSymbols && signingKnownAnswer(c);
        return c;
    }();
    return lc;
}

} // namespace

bool ed25519Available()
{
    return libcrypto().verifyReady;
}

bool ed25519Verify(const QByteArray& publicKey, const QByteArray& message, const QByteArray& signature)
{
    if (publicKey.size() != 32 || signature.size() != 64) {
        return false;
    }
    const LibCrypto& lc = libcrypto();
    if (!lc.verifyReady) {
        return false;
    }
    void* pkey = lc.newRawPublicKey(kEvpPkeyEd25519, nullptr,
                                    reinterpret_cast<const unsigned char*>(publicKey.constData()), 32);
    if (!pkey) {
        return false;
    }
    void* ctx = lc.mdCtxNew();
    bool ok = false;
    if (ctx && lc.digestVerifyInit(ctx, nullptr, nullptr, nullptr, pkey) == 1) {
        ok = lc.digestVerify(ctx,
                             reinterpret_cast<const unsigned char*>(signature.constData()), 64,
                             reinterpret_cast<const unsigned char*>(message.constData()),
                             static_cast<size_t>(message.size())) == 1;
    }
    if (ctx) {
        lc.mdCtxFree(ctx);
    }
    lc.pkeyFree(pkey);
    return ok;
}

QByteArray ed25519Sign(const QByteArray& privateSeed, const QByteArray& message)
{
    if (privateSeed.size() != 32) {
        return {};
    }
    const LibCrypto& lc = libcrypto();
    if (!lc.signReady) {
        return {};
    }
    void* pkey = lc.newRawPrivateKey(kEvpPkeyEd25519, nullptr,
                                     reinterpret_cast<const unsigned char*>(privateSeed.constData()), 32);
    if (!pkey) {
        return {};
    }
    void* ctx = lc.mdCtxNew();
    QByteArray out;
    if (ctx && lc.digestSignInit(ctx, nullptr, nullptr, nullptr, pkey) == 1) {
        size_t sigLen = 0;
        if (lc.digestSign(ctx, nullptr, &sigLen,
                          reinterpret_cast<const unsigned char*>(message.constData()),
                          static_cast<size_t>(message.size())) == 1 && sigLen > 0) {
            out.resize(static_cast<int>(sigLen));
            if (lc.digestSign(ctx, reinterpret_cast<unsigned char*>(out.data()), &sigLen,
                              reinterpret_cast<const unsigned char*>(message.constData()),
                              static_cast<size_t>(message.size())) == 1) {
                out.resize(static_cast<int>(sigLen));
            } else {
                out.clear();
            }
        }
    }
    if (ctx) {
        lc.mdCtxFree(ctx);
    }
    lc.pkeyFree(pkey);
    return out;
}

QByteArray ed25519DerivePublicKey(const QByteArray& privateSeed)
{
    if (privateSeed.size() != 32) {
        return {};
    }
    const LibCrypto& lc = libcrypto();
    if (!lc.signReady) {
        return {};
    }
    void* pkey = lc.newRawPrivateKey(kEvpPkeyEd25519, nullptr,
                                     reinterpret_cast<const unsigned char*>(privateSeed.constData()), 32);
    if (!pkey) {
        return {};
    }
    QByteArray pub(32, Qt::Uninitialized);
    size_t len = 32;
    const bool ok = lc.getRawPublicKey(pkey, reinterpret_cast<unsigned char*>(pub.data()), &len) == 1 && len == 32;
    lc.pkeyFree(pkey);
    return ok ? pub : QByteArray();
}

} // namespace orion
