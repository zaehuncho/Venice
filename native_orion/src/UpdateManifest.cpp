#include "UpdateManifest.h"

#include "Ed25519.h"
#include "LicenseClient.h" // compareSemanticVersions

#include <QtCore/QCryptographicHash>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>

#include <cctype>

namespace orion {

namespace {

// Constant-time comparison of two byte arrays. Avoids leaking, via timing, how
// many leading bytes of a candidate digest matched.
bool constantTimeEquals(const QByteArray& a, const QByteArray& b)
{
    if (a.size() != b.size()) {
        return false;
    }
    quint8 diff = 0;
    for (int i = 0; i < a.size(); ++i) {
        diff |= static_cast<quint8>(a.at(i)) ^ static_cast<quint8>(b.at(i));
    }
    return diff == 0;
}

QString stringField(const QJsonObject& obj, std::initializer_list<const char*> keys)
{
    for (const char* key : keys) {
        const auto value = obj.value(QLatin1String(key));
        if (value.isString() && !value.toString().trimmed().isEmpty()) {
            return value.toString().trimmed();
        }
    }
    return QString();
}

// Read a field that the server may emit as either a string or a number (e.g.
// published_at as an ISO string or a unix timestamp). Returned verbatim as a
// string so the signed canonical form is reproducible on both sides.
QString stringOrNumberField(const QJsonObject& obj, std::initializer_list<const char*> keys)
{
    for (const char* key : keys) {
        const auto value = obj.value(QLatin1String(key));
        if (value.isString() && !value.toString().trimmed().isEmpty()) {
            return value.toString().trimmed();
        }
        if (value.isDouble()) {
            return QString::number(static_cast<qint64>(value.toDouble()));
        }
    }
    return QString();
}

bool boolField(const QJsonObject& obj, std::initializer_list<const char*> keys, bool fallback)
{
    for (const char* key : keys) {
        const auto value = obj.value(QLatin1String(key));
        if (value.isBool()) {
            return value.toBool();
        }
        if (value.isDouble()) {
            return value.toInt() != 0;
        }
    }
    return fallback;
}

bool isHex64Chars(const QString& s)
{
    if (s.size() != 64) {
        return false;
    }
    for (const QChar c : s) {
        if (!std::isxdigit(static_cast<unsigned char>(c.toLatin1()))) {
            return false;
        }
    }
    return true;
}

// Decode a 64-byte Ed25519 signature from URL-safe base64 (the server format,
// padding optional), standard base64, or hex (128 chars).
QByteArray decodeSignatureBytes(const QString& encoded)
{
    const QString t = encoded.trimmed();
    if (t.size() == 128) {
        const QByteArray hex = QByteArray::fromHex(t.toLatin1());
        if (hex.size() == 64) {
            return hex;
        }
    }
    QByteArray b64 = QByteArray::fromBase64(t.toLatin1(), QByteArray::Base64UrlEncoding);
    if (b64.size() == 64) {
        return b64;
    }
    b64 = QByteArray::fromBase64(t.toLatin1(), QByteArray::Base64Encoding);
    if (b64.size() == 64) {
        return b64;
    }
    return {};
}

// Append a JSON string literal matching Python's json.dumps(ensure_ascii=True):
// quotes, backslash-escapes the short forms, and \uXXXX-escapes anything outside
// printable ASCII (0x20-0x7E) so the canonical bytes are reproducible byte-for-
// byte against the server signer.
void appendJsonString(QByteArray& out, const QString& value)
{
    out += '"';
    for (const QChar qc : value) {
        const ushort c = qc.unicode();
        switch (c) {
        case '"':  out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\b': out += "\\b"; break;
        case '\f': out += "\\f"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20 || c > 0x7E) {
                out += "\\u";
                out += QByteArray::number(c, 16).rightJustified(4, '0');
            } else {
                out += static_cast<char>(c);
            }
        }
    }
    out += '"';
}

} // namespace

UpdateManifest parseUpdateManifest(const QByteArray& payload)
{
    UpdateManifest manifest;
    manifest.raw = payload;

    QJsonParseError parseError;
    const auto doc = QJsonDocument::fromJson(payload, &parseError);
    if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
        manifest.message = QStringLiteral("Update manifest is not valid JSON.");
        return manifest;
    }

    const auto obj = doc.object();

    if (obj.contains(QStringLiteral("ok")) && !obj.value(QStringLiteral("ok")).toBool(true)) {
        manifest.message = stringField(obj, {"message", "error"});
        if (manifest.message.isEmpty()) {
            manifest.message = QStringLiteral("Update endpoint reported failure.");
        }
        return manifest;
    }

    manifest.version = stringField(obj, {"version", "latest", "latest_version"});
    manifest.minVersion = stringField(obj, {"minimum_supported_version", "min_version", "min_supported_version"});
    manifest.url = stringField(obj, {"url", "artifact_url", "download_url"});
    // Stored verbatim (not lowercased): the signature is computed over the exact
    // sha256 string the server emitted. Case-insensitive comparison happens in
    // verifyArtifactSha256.
    manifest.sha256 = stringField(obj, {"sha256", "artifact_sha256", "hash"});
    manifest.signature = stringField(obj, {"signature", "sig", "manifest_sig"});
    manifest.signatureAlg = stringField(obj, {"signature_alg", "sig_alg"}).toLower();
    manifest.publicKeyId = stringField(obj, {"public_key_id", "key_id", "pubkey_id"});
    manifest.publishedAt = stringOrNumberField(obj, {"published_at", "publishedAt", "published"});
    manifest.mandatory = boolField(obj, {"mandatory", "required", "force"}, false);
    manifest.allowRollback = boolField(obj, {"allow_rollback", "allow_downgrade", "rollback"}, false);
    manifest.channel = stringField(obj, {"channel", "ring"}).toLower();
    manifest.notes = stringField(obj, {"notes", "release_notes", "changelog"});

    if (manifest.version.isEmpty()) {
        manifest.message = QStringLiteral("Update manifest is missing the version field.");
        return manifest;
    }

    manifest.ok = true;
    manifest.message = QStringLiteral("Update manifest parsed.");
    return manifest;
}

QByteArray canonicalManifestSigningString(const UpdateManifest& manifest)
{
    // Byte-exact reproduction of the server signer: compact JSON of the eight
    // signed fields in this fixed order, lowercase boolean literals, no
    // whitespace, ASCII-escaped strings (Python json.dumps(sort_keys=False,
    // separators=(",", ":"), ensure_ascii=True)). See docs/UPDATER_CLIENT.md.
    QByteArray out;
    out += "{\"latest_version\":";
    appendJsonString(out, manifest.version);
    out += ",\"minimum_supported_version\":";
    appendJsonString(out, manifest.minVersion);
    out += ",\"artifact_url\":";
    appendJsonString(out, manifest.url);
    out += ",\"sha256\":";
    appendJsonString(out, manifest.sha256);
    out += ",\"published_at\":";
    appendJsonString(out, manifest.publishedAt);
    out += ",\"mandatory\":";
    out += manifest.mandatory ? "true" : "false";
    out += ",\"allow_rollback\":";
    out += manifest.allowRollback ? "true" : "false";
    out += ",\"public_key_id\":";
    appendJsonString(out, manifest.publicKeyId);
    out += "}";
    return out;
}

QByteArray decodeEd25519PublicKey(const QString& encoded)
{
    const QString t = encoded.trimmed();
    if (isHex64Chars(t)) {
        const QByteArray hex = QByteArray::fromHex(t.toLatin1());
        if (hex.size() == 32) {
            return hex;
        }
    }
    QByteArray b64 = QByteArray::fromBase64(t.toLatin1());
    if (b64.size() == 32) {
        return b64;
    }
    b64 = QByteArray::fromBase64(t.toLatin1(), QByteArray::Base64UrlEncoding);
    if (b64.size() == 32) {
        return b64;
    }
    return {};
}

bool verifyManifestSignature(const UpdateManifest& manifest, const QByteArray& publicKey)
{
    if (manifest.signatureAlg.compare(QLatin1String("ed25519"), Qt::CaseInsensitive) != 0) {
        return false;
    }
    if (manifest.publicKeyId.trimmed().isEmpty()) {
        return false;
    }
    if (manifest.signature.trimmed().isEmpty()) {
        return false;
    }
    if (publicKey.size() != 32) {
        return false;
    }
    const QByteArray sig = decodeSignatureBytes(manifest.signature);
    if (sig.size() != 64) {
        return false;
    }
    return ed25519Verify(publicKey, canonicalManifestSigningString(manifest), sig);
}

bool verifyArtifactSha256(const QByteArray& artifact, const QString& expectedHex)
{
    const QString normalized = expectedHex.trimmed().toLower();
    if (normalized.size() != 64) {
        return false;
    }
    const QByteArray actual = QCryptographicHash::hash(artifact, QCryptographicHash::Sha256).toHex();
    return constantTimeEquals(actual, normalized.toLatin1());
}

UpdateDecision evaluateUpdate(const QString& currentVersion, const UpdateManifest& manifest)
{
    if (!manifest.ok) {
        return UpdateDecision::InvalidManifest;
    }

    if (!manifest.minVersion.isEmpty() && compareSemanticVersions(currentVersion, manifest.minVersion) < 0) {
        return UpdateDecision::ClientBlocked;
    }

    const int cmp = compareSemanticVersions(currentVersion, manifest.version);
    if (cmp < 0) {
        return manifest.mandatory ? UpdateDecision::MandatoryUpdate : UpdateDecision::UpdateAvailable;
    }
    if (cmp > 0) {
        return manifest.allowRollback ? UpdateDecision::UpdateAvailable : UpdateDecision::DowngradeBlocked;
    }
    return UpdateDecision::UpToDate;
}

UpdateGateAction evaluateUpdateGate(UpdateDecision decision, bool devBuild, bool updaterPresent)
{
    switch (decision) {
    case UpdateDecision::UpToDate:
    case UpdateDecision::DowngradeBlocked:
    case UpdateDecision::InvalidManifest:
        return UpdateGateAction::Proceed;
    case UpdateDecision::UpdateAvailable:
    case UpdateDecision::MandatoryUpdate:
    case UpdateDecision::ClientBlocked:
        break;
    }
    // Dev guard: a build-tree launch never auto-applies and is always escapable,
    // even for mandatory/blocked manifests (a dev checkout must not brick itself).
    if (devBuild) {
        return UpdateGateAction::OfferUpdate;
    }
    if (!updaterPresent) {
        // Cannot apply anything without OrionUpdater.exe. An optional update
        // degrades to the status pill; a server hard block still gates so the
        // user sees WHY the client refuses to run (broken install, reinstall).
        return decision == UpdateDecision::ClientBlocked ? UpdateGateAction::ForceUpdate
                                                         : UpdateGateAction::Proceed;
    }
    return decision == UpdateDecision::UpdateAvailable ? UpdateGateAction::OfferUpdate
                                                       : UpdateGateAction::ForceUpdate;
}

} // namespace orion
