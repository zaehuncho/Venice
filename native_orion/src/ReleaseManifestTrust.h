#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QString>

namespace orion {

// Public trust anchor shared with OrionUpdater's initial update-signing key.
// Public keys are not secret; the private key must remain server/operator-side.
inline constexpr char kReleaseManifestPublicKeyId[] = "orion-ed25519-v1";
inline constexpr char kReleaseManifestPublicKeyB64[] =
    "OJQ2E7ZAFClOCM4S4/5QzLeQjjEMSZjPiGMzbiLZdWs=";

// Pure policy helper kept separate from environment access so tests can prove
// that production never honors a developer/test trust override.
[[nodiscard]] inline QByteArray selectReleaseManifestPublicKeyEncoding(
    bool productionBuild,
    const QString& requestedKeyId,
    const QString& testOverrideKeyId = {},
    const QByteArray& testOverrideKeyB64 = {})
{
    if (!productionBuild
        && !testOverrideKeyId.isEmpty()
        && !testOverrideKeyB64.isEmpty()
        && requestedKeyId == testOverrideKeyId) {
        return testOverrideKeyB64;
    }
    if (requestedKeyId == QLatin1String(kReleaseManifestPublicKeyId)) {
        return QByteArray(kReleaseManifestPublicKeyB64);
    }
    return {};
}

} // namespace orion
