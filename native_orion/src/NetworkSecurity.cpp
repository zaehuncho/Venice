#include "NetworkSecurity.h"

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
const char* const kProductionApiPinnedSpkiSha256[] = {
    // ── Google Trust Services (current issuer) ──
    "908769e8d34477cc2cba0632c88605b22d7294c0840f78596d247c645b1afc0e", // GTS WE1 (live issuer; primary pin)
    "be1efc292835472e0d6aa183575d30fdc4dbf551f7050519a0258d6bddd6fc46", // GTS WE2 (ECC sibling, backup)
    "9847e5653e5e9e847516e5cb818606aa7544a19be67fd7366d506988e8d84347", // GTS Root R4 (served in the live chain)
    // ── Let's Encrypt / ISRG (Cloudflare Universal SSL fallback CA) ──
    "3586d4ecf070578cbd27aedce20b964e48bc149faeb9dad72f46b857869172b8", // E5
    "d016e1fe311948aca64f2de44ce86c9a51ca041df6103bb52a88eb3f761f57d7", // E6
    "cbbc559b44d524d6a132bdac672744da3407f12aae5d5f722c5f6c7913871c75", // E7
    "885bf0572252c6741dc9a52f5044487fef2a93b811cdedfad7624cc283b7cdd5", // E8
    "f1440a9b76e1e41e53a4cb461329bf6337b419726be513e42e19f1c691c5d4b2", // E9
    "2bbad93ab5c79279ec121507f272cbe0c6647a3aae52e22f388afab426b4adba", // R10
    "6ddac18698f7f1f7e1c69b9bce420d974ac6f94ca8b2c761701623f99c767dc7", // R11
    "919c0df7a787b597ed056ace654b1de9c0387acf349f73734a4fd7b58cf612a4", // R12
    "025490860b498ab73c6a12f27a49ad5fe230fafe3ac8f6112c9b7d0aad46941d", // R13
    "f1647a5ee3efac54c892e930584fe47979b7acd1c76c1271bca1c5076d869888", // R14
};

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
    pins.reserve(static_cast<qsizetype>(std::size(kProductionApiPinnedSpkiSha256)));
    for (const char* pin : kProductionApiPinnedSpkiSha256) {
        pins.append(QString::fromLatin1(pin));
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
