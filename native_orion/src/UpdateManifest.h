#pragma once

#include "OrionExports.h"

#include <QtCore/QByteArray>
#include <QtCore/QMetaType>
#include <QtCore/QString>

namespace orion {

// Parsed /api/update (or standalone manifest.json) payload. The Ed25519
// signature covers the security-critical fields so a tampered URL/hash/version/
// policy is rejected before any download is executed. See docs/UPDATER_CLIENT.md
// for the wire contract and the exact canonical-signing-string layout.
struct UpdateManifest {
    bool ok = false;             // payload parsed and required fields present
    QString version;            // latest available client version (e.g. "1.0.4")
    QString minVersion;         // minimum_supported_version: clients below are blocked
    QString url;                // HTTPS artifact URL (zip)
    QString sha256;             // expected lowercase hex SHA-256 of the artifact
    QString signature;          // Ed25519 signature over the canonical string (hex or base64)
    QString signatureAlg;       // must be "ed25519" for production verification
    QString publicKeyId;        // identifies which trusted public key signed this
    QString publishedAt;        // server publish stamp, signed verbatim
    bool mandatory = false;     // server flags this update as mandatory
    bool allowRollback = false; // server explicitly permits installing an older version
    QString channel;            // release ring this manifest was served for (dev/internal/beta/stable).
                                // Informational/display only: NOT part of the signed canonical string —
                                // ring routing is enforced server-side by serving per-ring manifests.
    QString notes;              // human-readable release notes (optional)
    QString message;            // parse/validation error or status detail
    QByteArray raw;             // original payload bytes (for diagnostics, never logged)
};

// What the launcher should do given the local version and a verified manifest.
enum class UpdateDecision {
    UpToDate,           // local >= latest and not below minimum
    UpdateAvailable,    // newer version offered, optional
    MandatoryUpdate,    // newer version offered and server marks it mandatory
    DowngradeBlocked,   // manifest version is older and rollback not allowed
    ClientBlocked,      // local version < minimum_supported_version
    InvalidManifest     // manifest did not parse / failed validation
};

// Parse the JSON body of /api/update into an UpdateManifest. Tolerant of the
// field-name variants used elsewhere in the backend (latest/version,
// minimum_supported_version/min_version, artifact_url/download_url, etc.).
// Never throws.
ORION_SECURITY_API UpdateManifest parseUpdateManifest(const QByteArray& payload);

// Exact bytes the Ed25519 signature is computed over. Deterministic and easy for
// the server to reproduce. The following eight values are joined by '\n'
// (newline 0x0A) with NO trailing newline:
//   version
//   minimum_supported_version
//   url (artifact_url)
//   sha256 (lowercased)
//   mandatory      ("1" or "0")
//   allow_rollback ("1" or "0")
//   published_at
//   public_key_id
ORION_SECURITY_API QByteArray canonicalManifestSigningString(const UpdateManifest& manifest);

// Decode a 32-byte Ed25519 public key from hex (64 chars) or base64; empty on
// failure. Public keys are not secret and ship with the client.
ORION_SECURITY_API QByteArray decodeEd25519PublicKey(const QString& encoded);

// Verify the manifest's Ed25519 signature against a resolved 32-byte public key.
// Fails closed unless ALL hold: signature_alg == "ed25519", public_key_id is
// present, the signature decodes to 64 bytes, publicKey is 32 bytes, and the
// signature is valid over canonicalManifestSigningString(manifest). The caller
// resolves public_key_id to the trusted public key (an unknown id yields an
// empty key here, which fails).
ORION_SECURITY_API bool verifyManifestSignature(const UpdateManifest& manifest, const QByteArray& publicKey);

// Verify that the downloaded artifact bytes hash to the manifest's sha256.
// Case-insensitive hex compare, constant time over the digest.
ORION_SECURITY_API bool verifyArtifactSha256(const QByteArray& artifact, const QString& expectedHex);

// Decide what the launcher should do. Pure policy: does not perform any download.
ORION_SECURITY_API UpdateDecision evaluateUpdate(const QString& currentVersion, const UpdateManifest& manifest);

// What the startup update GATE (the full-screen view shown before AuthGate)
// should do for a given decision.
enum class UpdateGateAction {
    Proceed,     // no gate: continue to AuthGate on the current version
    OfferUpdate, // gate with auto-proceed countdown AND a "continue without updating" escape
    ForceUpdate  // gate with auto-proceed countdown and NO escape (mandatory / blocked)
};

// Pure gate policy. devBuild = running from a build tree (dev guard: never
// auto-apply, always escapable so a dev checkout cannot brick itself).
// updaterPresent = OrionUpdater.exe exists next to the launcher (without it an
// update cannot be applied, so an optional update degrades to the status pill
// and only a hard server block still gates).
ORION_SECURITY_API UpdateGateAction evaluateUpdateGate(UpdateDecision decision, bool devBuild, bool updaterPresent);

} // namespace orion

Q_DECLARE_METATYPE(orion::UpdateManifest)
