#include "NetworkSecurity.h"

#include "PinnedSpki.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QList>
#include <QtNetwork/QNetworkReply>
#include <QtNetwork/QNetworkRequest>
#include <QtNetwork/QSslCertificate>
#include <QtNetwork/QSslConfiguration>
#include <QtNetwork/QSslKey>
#include <QtNetwork/QSslSocket>

#include <iterator>

namespace orion {

namespace {

constexpr auto kProductionApiHost = "api.zaeorion.com";
constexpr auto kProductionApiBaseUrl = "https://api.zaeorion.com";

// MED-3 (docs/SECURITY_REDTEAM.md): pin the ISSUING-CA SubjectPublicKeyInfo
// (SPKI) SHA-256 instead of the leaf certificate DER. Cloudflare auto-renews
// the ~90-day edge leaf, so a leaf-DER pin either self-bricks on renewal or
// (the previous state) has to fail open and protects nothing. The intermediate
// KEY is stable across those renewals — the live chain's "GTS WE1" and Google's
// published i.pki.goog/we1.crt are two different cert DERs carrying the SAME
// SPKI — so an SPKI pin is real (fails CLOSED) yet survives rotation.
//
// The check passes iff ANY certificate in the verified TLS chain has an SPKI
// SHA-256 in this set. TLS peer verification (VerifyPeer + PeerVerifyName in
// applyStrictTls) still runs first; the pin narrows "any public CA" down to
// the CAs Cloudflare actually issues from for this zone.
//
// Values computed 2026-07-09 from the live api.zaeorion.com chain and
// cross-checked against the CA-published certificates (i.pki.goog,
// letsencrypt.org/certs). Cloudflare Universal SSL issues from Google Trust
// Services today and can fail over to Let's Encrypt, so both families are
// pinned (primary + disaster-recovery backups). Rotate this set during release
// prep if Cloudflare ever moves to another CA family (e.g. SSL.com).
//
// [SERVER-SHARD blocker #5] The pin VALUES now live in the shared header
// src/PinnedSpki.h (orion::kApiPinnedSpkiSha256) so the WinHTTP activation
// broker and the Lethe shard fetch pin the SAME issuer keys as this client.
// Kept here as an aliased reference; do not re-list the hex here.
const char* const* const kProductionApiPinnedSpkiSha256 = orion::kApiPinnedSpkiSha256;
constexpr std::size_t kProductionApiPinnedSpkiSha256Count = orion::kApiPinnedSpkiSha256Count;

} // namespace

QString productionApiBaseUrl()
{
    return QString::fromLatin1(kProductionApiBaseUrl);
}

QString productionApiPinnedCertificateSha256()
{
    // Kept as the LicenseClient/AdminToolController "pinning enabled" marker;
    // the actual match is the SPKI pin set (primary pin returned here).
    return QString::fromLatin1(kProductionApiPinnedSpkiSha256[0]);
}

QStringList productionApiPinnedSpkiSha256()
{
    QStringList pins;
    pins.reserve(static_cast<qsizetype>(kProductionApiPinnedSpkiSha256Count));
    for (std::size_t i = 0; i < kProductionApiPinnedSpkiSha256Count; ++i) {
        pins.append(QString::fromLatin1(kProductionApiPinnedSpkiSha256[i]));
    }
    return pins;
}

void applyStrictTls(QNetworkRequest& request)
{
    auto ssl = QSslConfiguration::defaultConfiguration();
    ssl.setPeerVerifyMode(QSslSocket::VerifyPeer);
    request.setSslConfiguration(ssl);
    request.setPeerVerifyName(QString::fromLatin1(kProductionApiHost));
}

QByteArray certificateSpkiDer(const QSslCertificate& certificate)
{
    const QSslKey key = certificate.publicKey();
    if (key.isNull()) {
        return {};
    }
    // "-----BEGIN PUBLIC KEY-----" PEM bodies ARE the DER SubjectPublicKeyInfo
    // (X.509 SPKI) for both RSA and EC keys, so decoding the PEM body yields
    // exactly the bytes RFC 7469-style SPKI pins hash.
    const QByteArray pem = key.toPem();
    if (pem.isEmpty()) {
        return {};
    }
    QByteArray base64Body;
    base64Body.reserve(pem.size());
    for (const QByteArray& line : pem.split('\n')) {
        const QByteArray trimmed = line.trimmed();
        if (trimmed.isEmpty() || trimmed.startsWith("-----")) {
            continue;
        }
        base64Body += trimmed;
    }
    return QByteArray::fromBase64(base64Body);
}

QString certificateSpkiSha256Hex(const QSslCertificate& certificate)
{
    const QByteArray spki = certificateSpkiDer(certificate);
    if (spki.isEmpty()) {
        return {};
    }
    return QString::fromLatin1(QCryptographicHash::hash(spki, QCryptographicHash::Sha256).toHex()).toLower();
}

bool certificateChainMatchesPinnedSpki(const QList<QSslCertificate>& chain, QString* detail)
{
    if (chain.isEmpty()) {
        if (detail) *detail = QStringLiteral("TLS peer certificate chain is empty.");
        return false;
    }
    const QStringList pins = productionApiPinnedSpkiSha256();
    for (const QSslCertificate& certificate : chain) {
        const QString spkiHex = certificateSpkiSha256Hex(certificate);
        if (!spkiHex.isEmpty() && pins.contains(spkiHex)) {
            if (detail) {
                *detail = QStringLiteral("Pinned issuer SPKI matched (%1, %2)")
                              .arg(certificate.subjectDisplayName(), spkiHex.left(16));
            }
            return true;
        }
    }
    // FAIL CLOSED. The pre-2026-07-09 code logged a leaf mismatch and returned
    // true ("advisory pin"), which made the pin decorative. An unexpected chain
    // now aborts the request — rotate the pin set above if the CA ever changes.
    if (detail) {
        *detail = QStringLiteral("No certificate in the TLS chain matched a pinned issuer SPKI (chain length %1).")
                      .arg(chain.size());
    }
    return false;
}

bool replyMatchesPinnedCertificate(const QNetworkReply* reply, QString* detail)
{
    if (!reply) {
        if (detail) *detail = QStringLiteral("No network reply to inspect.");
        return false;
    }
    return certificateChainMatchesPinnedSpki(reply->sslConfiguration().peerCertificateChain(), detail);
}

} // namespace orion
