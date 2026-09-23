#include "BrokerInstallTrust.h"

#include "Ed25519.h"
#include "ReleaseManifestTrust.h"
#include "UpdateManifest.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QLatin1String>

#include <cctype>

#ifdef Q_OS_WIN
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <wincrypt.h>
#include <wintrust.h>
#include <softpub.h>

#pragma comment(lib, "wintrust.lib")
#pragma comment(lib, "crypt32.lib")
#endif

namespace orion {

namespace {

// Detached 64-byte Ed25519 signature decode: 128-hex OR base64url OR standard
// base64. Mirrors SecurityManager.cpp's file-local decodeDetachedEd25519Signature
// (which is in an anonymous namespace and cannot be linked). Kept byte-compatible
// so a signature produced by the packer verifies identically on both paths.
QByteArray decodeDetached64(const QByteArray& encoded)
{
    const QByteArray text = encoded.trimmed();
    if (text.size() == 128) {
        bool hexOnly = true;
        for (const char c : text) {
            if (!std::isxdigit(static_cast<unsigned char>(c))) {
                hexOnly = false;
                break;
            }
        }
        if (hexOnly) {
            const QByteArray raw = QByteArray::fromHex(text);
            if (raw.size() == 64) {
                return raw;
            }
        }
    }
    QByteArray raw = QByteArray::fromBase64(text, QByteArray::Base64UrlEncoding);
    if (raw.size() == 64) {
        return raw;
    }
    raw = QByteArray::fromBase64(text, QByteArray::Base64Encoding);
    return raw.size() == 64 ? raw : QByteArray{};
}

#ifdef Q_OS_WIN
// Read the Authenticode signer's simple display name from a signed PE, and the
// leaf cert's SHA-1 thumbprint (uppercase hex, no spaces). Ported verbatim from
// the previous main.cpp implementation so there is exactly ONE copy.
QString authenticodeSigner(const QString& path, QString* thumbprintOut)
{
    if (thumbprintOut) thumbprintOut->clear();
    const std::wstring wpath = path.toStdWString();
    HCERTSTORE hStore = nullptr;
    HCRYPTMSG hMsg = nullptr;
    DWORD encoding = 0, contentType = 0, formatType = 0;
    QString name;
    if (!CryptQueryObject(CERT_QUERY_OBJECT_FILE, wpath.c_str(),
                          CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
                          CERT_QUERY_FORMAT_FLAG_BINARY, 0,
                          &encoding, &contentType, &formatType,
                          &hStore, &hMsg, nullptr) || !hMsg || !hStore) {
        if (hStore) CertCloseStore(hStore, 0);
        if (hMsg) CryptMsgClose(hMsg);
        return name;
    }
    DWORD signerInfoSize = 0;
    if (CryptMsgGetParam(hMsg, CMSG_SIGNER_INFO_PARAM, 0, nullptr, &signerInfoSize)
        && signerInfoSize > 0) {
        QByteArray buf(int(signerInfoSize), Qt::Uninitialized);
        auto* info = reinterpret_cast<CMSG_SIGNER_INFO*>(buf.data());
        if (CryptMsgGetParam(hMsg, CMSG_SIGNER_INFO_PARAM, 0, info, &signerInfoSize)) {
            CERT_INFO ci{};
            ci.Issuer = info->Issuer;
            ci.SerialNumber = info->SerialNumber;
            if (PCCERT_CONTEXT cert = CertFindCertificateInStore(
                    hStore, encoding, 0, CERT_FIND_SUBJECT_CERT, &ci, nullptr)) {
                const DWORD n = CertGetNameStringW(
                    cert, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, nullptr, nullptr, 0);
                if (n > 1) {
                    std::wstring out(n, L'\0');
                    CertGetNameStringW(cert, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0,
                                       nullptr, out.data(), n);
                    name = QString::fromWCharArray(out.c_str()).trimmed();
                }
                if (thumbprintOut) {
                    BYTE tp[20] = {};
                    DWORD tpLen = sizeof(tp);
                    if (CertGetCertificateContextProperty(
                            cert, CERT_SHA1_HASH_PROP_ID, tp, &tpLen)) {
                        *thumbprintOut = QString::fromLatin1(
                            QByteArray(reinterpret_cast<char*>(tp), int(tpLen)).toHex()).toUpper();
                    }
                }
                CertFreeCertificateContext(cert);
            }
        }
    }
    CertCloseStore(hStore, 0);
    CryptMsgClose(hMsg);
    return name;
}
#endif // Q_OS_WIN

} // namespace

bool fileAuthenticodeSignedByVenice(const QString& path, QString* detail)
{
#ifdef Q_OS_WIN
    // [FIX B — 2026-09-20 hardening round 3] The Authenticode gate is INERT unless
    // the OWNER has pinned an EXACT 40-hex leaf thumbprint in
    // kExpectedBrokerSignerThumbprint. With it EMPTY (the current state — the owner
    // is NOT code-signing for launch), this branch does nothing and returns false,
    // so the SIGNED release manifest (gate B) is the SOLE trust anchor. A signer
    // whose subject merely CONTAINS "Venice" is NEVER accepted on its own: without
    // the exact thumbprint pin any WinVerifyTrust-valid exe with a matching signer
    // name would otherwise pass WITHOUT manifest coverage (Codex finding). Setting a
    // real 40-hex thumbprint later re-enables gate (A) as defense-in-depth.
    const QString expectedThumb =
        QString::fromWCharArray(kExpectedBrokerSignerThumbprint).trimmed();
    const bool thumbIs40Hex = [&expectedThumb]() {
        if (expectedThumb.size() != 40) return false;
        for (const QChar c : expectedThumb) {
            const ushort u = c.unicode();
            const bool isHex = (u >= u'0' && u <= u'9') || (u >= u'a' && u <= u'f')
                            || (u >= u'A' && u <= u'F');
            if (!isHex) return false;
        }
        return true;
    }();
    if (!thumbIs40Hex) {
        if (detail) *detail = QStringLiteral(
            "authenticode gate inert: no pinned 40-hex leaf thumbprint "
            "(signed release manifest is the sole trust anchor)");
        return false; // gate (A) disabled -> gate (B), the signed manifest, authorizes
    }

    if (!QFileInfo::exists(path)) {
        if (detail) *detail = QStringLiteral("broker file missing");
        return false;
    }
    const std::wstring wpath = path.toStdWString();
    WINTRUST_FILE_INFO fileInfo{};
    fileInfo.cbStruct = sizeof(fileInfo);
    fileInfo.pcwszFilePath = wpath.c_str();
    WINTRUST_DATA data{};
    data.cbStruct = sizeof(data);
    data.dwUIChoice = WTD_UI_NONE;
    data.fdwRevocationChecks = WTD_REVOKE_NONE;
    data.dwUnionChoice = WTD_CHOICE_FILE;
    data.pFile = &fileInfo;
    data.dwStateAction = WTD_STATEACTION_VERIFY;
    data.dwProvFlags = WTD_SAFER_FLAG | WTD_CACHE_ONLY_URL_RETRIEVAL;
    GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    const LONG status = WinVerifyTrust(static_cast<HWND>(INVALID_HANDLE_VALUE), &action, &data);
    data.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(static_cast<HWND>(INVALID_HANDLE_VALUE), &action, &data);
    if (status != ERROR_SUCCESS) {
        if (detail) *detail = QStringLiteral("unsigned or untrusted");
        return false; // unsigned or untrusted -> not a genuine signed broker
    }
    QString thumbprint;
    const QString signer = authenticodeSigner(path, &thumbprint);
    // The gate is active only with a valid 40-hex pin (checked above), so the EXACT
    // leaf thumbprint is MANDATORY here — subject matching alone is never enough.
    if (thumbprint.compare(expectedThumb, Qt::CaseInsensitive) != 0) {
        if (detail) *detail = QStringLiteral("signer thumbprint mismatch");
        return false;
    }
    const QString expectedSubstr = QString::fromWCharArray(kExpectedBrokerSignerSubstr).trimmed();
    if (expectedSubstr.isEmpty()) {
        if (detail) *detail = QStringLiteral("expected signer substring not configured");
        return false; // misconfiguration: never trust on an unbounded signer
    }
    const bool ok = signer.contains(expectedSubstr, Qt::CaseInsensitive);
    if (!ok && detail) *detail = QStringLiteral("signer subject mismatch");
    return ok;
#else
    Q_UNUSED(path);
    if (detail) *detail = QStringLiteral("authenticode unsupported on this platform");
    return false;
#endif
}

bool releaseManifestCoversFileSigned(const QString& manifestDir,
                                     const QString& targetFilePath,
                                     bool productionBuild,
                                     const QString& testOverrideKeyId,
                                     const QByteArray& testOverrideKeyB64,
                                     QString* detail)
{
    const QDir dir(manifestDir);
    const QString manifestPath = dir.absoluteFilePath(QStringLiteral("release_manifest.json"));

    QFile mf(manifestPath);
    if (!mf.open(QIODevice::ReadOnly)) {
        if (detail) *detail = QStringLiteral("release manifest missing");
        return false;
    }
    // Preserve exact bytes: the detached signature authenticates THIS byte
    // sequence, not a re-serialized approximation.
    const QByteArray manifestBytes = mf.read(4 * 1024 * 1024);
    mf.close();

    QJsonParseError perr{};
    const QJsonDocument doc = QJsonDocument::fromJson(manifestBytes, &perr);
    if (manifestBytes.isEmpty() || perr.error != QJsonParseError::NoError || !doc.isObject()) {
        if (detail) *detail = QStringLiteral("release manifest invalid JSON");
        return false;
    }
    const QJsonObject manifest = doc.object();

    if (manifest.value(QStringLiteral("schema")).toString()
            != QLatin1String("orion.release_manifest.v1")) {
        if (detail) *detail = QStringLiteral("release manifest schema mismatch");
        return false;
    }
    // The server-shard broker ships in the CUSTOMER package.
    if (manifest.value(QStringLiteral("audience")).toString().trimmed()
            != QLatin1String("customer")) {
        if (detail) *detail = QStringLiteral("release manifest audience is not customer");
        return false;
    }
    // Signature is MANDATORY for the broker trust gate — no dev-optional path.
    if (!manifest.value(QStringLiteral("signature_required")).toBool(false)) {
        if (detail) *detail = QStringLiteral("release manifest signature_required is not true");
        return false;
    }
    if (manifest.value(QStringLiteral("signature_alg")).toString().trimmed().compare(
            QLatin1String("ed25519"), Qt::CaseInsensitive) != 0) {
        if (detail) *detail = QStringLiteral("release manifest signature_alg mismatch");
        return false;
    }
    const QString keyId = manifest.value(QStringLiteral("public_key_id")).toString().trimmed();
    if (keyId != QLatin1String(kReleaseManifestPublicKeyId)) {
        if (detail) *detail = QStringLiteral("release manifest public_key_id is not the pinned id");
        return false;
    }

    const QString sigPath = dir.absoluteFilePath(QStringLiteral("release_manifest.sig"));
    QFile sf(sigPath);
    if (!sf.open(QIODevice::ReadOnly)) {
        if (detail) *detail = QStringLiteral("release manifest signature missing");
        return false;
    }
    const QByteArray sigEncoded = sf.read(4096);
    sf.close();
    const QByteArray signature = decodeDetached64(sigEncoded);
    if (signature.size() != 64) {
        if (detail) *detail = QStringLiteral("release manifest signature undecodable");
        return false;
    }

    // Resolve the PINNED public key. In a production build the test override is
    // ignored (selectReleaseManifestPublicKeyEncoding enforces that).
    const QByteArray publicKey = decodeEd25519PublicKey(QString::fromLatin1(
        selectReleaseManifestPublicKeyEncoding(
            productionBuild, keyId, testOverrideKeyId, testOverrideKeyB64)));
    if (publicKey.size() != 32) {
        if (detail) *detail = QStringLiteral("release manifest public key not trusted");
        return false;
    }
    if (!ed25519Available()) {
        if (detail) *detail = QStringLiteral("Ed25519 verifier unavailable (libcrypto missing/altered)");
        return false;
    }
    if (!ed25519Verify(publicKey, manifestBytes, signature)) {
        if (detail) *detail = QStringLiteral("release manifest signature verification failed");
        return false;
    }

    // Signature valid — now require exact SHA-256 coverage of the target file.
    const QJsonObject files = manifest.value(QStringLiteral("files")).toObject();
    if (files.isEmpty()) {
        if (detail) *detail = QStringLiteral("release manifest lists no files");
        return false;
    }
    const QString base = QFileInfo(targetFilePath).fileName();
    QString expected;
    for (auto it = files.constBegin(); it != files.constEnd(); ++it) {
        if (QFileInfo(QDir::fromNativeSeparators(it.key())).fileName()
                .compare(base, Qt::CaseInsensitive) == 0) {
            expected = it.value().isString()
                ? it.value().toString().trimmed().toLower()
                : it.value().toObject().value(QStringLiteral("sha256")).toString().trimmed().toLower();
            break;
        }
    }
    if (expected.size() != 64) {
        if (detail) *detail = QStringLiteral("target file not covered by manifest");
        return false;
    }

    QFile bf(targetFilePath);
    if (!bf.open(QIODevice::ReadOnly)) {
        if (detail) *detail = QStringLiteral("target file unreadable");
        return false;
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&bf)) {
        if (detail) *detail = QStringLiteral("target file hash failed");
        return false;
    }
    const QString actual = QString::fromLatin1(hash.result().toHex()).toLower();
    const bool ok = (actual == expected);
    if (!ok && detail) *detail = QStringLiteral("target file hash mismatch");
    return ok;
}

bool genuineBrokerInstallPresent(const QString& brokerExePath,
                                 const QString& manifestDir,
                                 bool productionBuild,
                                 const QString& testOverrideKeyId,
                                 const QByteArray& testOverrideKeyB64,
                                 QString* detail)
{
    if (!QFileInfo::exists(brokerExePath)) {
        if (detail) *detail = QStringLiteral("broker exe absent");
        return false;
    }
    // Gate (A): validly Authenticode-signed by Venice.
    if (fileAuthenticodeSignedByVenice(brokerExePath, detail)) {
        return true;
    }
    // Gate (B): byte-covered by a signed release manifest.
    return releaseManifestCoversFileSigned(manifestDir, brokerExePath, productionBuild,
                                           testOverrideKeyId, testOverrideKeyB64, detail);
}

} // namespace orion
