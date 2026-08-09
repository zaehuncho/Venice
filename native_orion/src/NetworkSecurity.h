#pragma once

#include "OrionExports.h"

#include <QtCore/QByteArray>
#include <QtCore/QList>
#include <QtCore/QString>
#include <QtCore/QStringList>

class QNetworkReply;
class QNetworkRequest;
class QSslCertificate;

namespace orion {

ORION_SECURITY_API QString productionApiBaseUrl();
// Primary pin (kept as the callers' "pinning enabled" marker). Since the MED-3
// fix this is the GTS WE1 issuing-CA SPKI SHA-256, not a leaf DER hash.
ORION_SECURITY_API QString productionApiPinnedCertificateSha256();
// Full issuer-SPKI pin set (GTS primary + Let's Encrypt backups).
ORION_SECURITY_API QStringList productionApiPinnedSpkiSha256();
ORION_SECURITY_API void applyStrictTls(QNetworkRequest& request);
// DER SubjectPublicKeyInfo of a certificate's public key (empty on failure).
ORION_SECURITY_API QByteArray certificateSpkiDer(const QSslCertificate& certificate);
// Lowercase hex SHA-256 of the certificate's SPKI (empty on failure).
ORION_SECURITY_API QString certificateSpkiSha256Hex(const QSslCertificate& certificate);
// True iff ANY certificate in the chain carries a pinned issuer SPKI. Fails
// CLOSED on an empty chain or no match (MED-3: the pin is real again).
ORION_SECURITY_API bool certificateChainMatchesPinnedSpki(const QList<QSslCertificate>& chain,
                                                          QString* detail = nullptr);
ORION_SECURITY_API bool replyMatchesPinnedCertificate(const QNetworkReply* reply, QString* detail = nullptr);

} // namespace orion
