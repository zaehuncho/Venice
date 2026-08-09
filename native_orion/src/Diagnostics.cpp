#include "Diagnostics.h"

#include <QtCore/QRegularExpression>

namespace orion {

QString redactDiagnosticsText(const QString& text)
{
    QString out = text;

    // License keys: ORION-XXXX-XXXX-XXXX / NVDEV-... — keep the last group so
    // support can correlate without exposing the key.
    static const QRegularExpression licenseKey(
        QStringLiteral("\\b(ORION|NVDEV)(-[A-Za-z0-9]{3,8}){2,}\\b"));
    {
        QRegularExpressionMatchIterator it = licenseKey.globalMatch(out);
        // Collect first (replacing while iterating would invalidate offsets).
        QStringList found;
        while (it.hasNext()) {
            found << it.next().captured(0);
        }
        for (const QString& key : found) {
            const QString suffix = key.section(QLatin1Char('-'), -1);
            out.replace(key, key.section(QLatin1Char('-'), 0, 0) + QStringLiteral("-...-") + suffix);
        }
    }

    // JWTs (three dot-separated base64url segments) before generic token labels,
    // so the value itself disappears even without a label.
    static const QRegularExpression jwt(
        QStringLiteral("\\beyJ[A-Za-z0-9_-]{6,}\\.[A-Za-z0-9_-]{6,}\\.[A-Za-z0-9_-]{6,}\\b"));
    out.replace(jwt, QStringLiteral("<redacted-jwt>"));

    // Labelled secrets: token=..., "access_token": "...", authorization: bearer ...,
    // machine_id=..., api_key: ..., secret=..., password=...
    static const QRegularExpression labelled(
        QStringLiteral("(?i)\\b(token|access_token|refresh_token|authorization|bearer|api[_-]?key|secret|password|machine[_-]?id|license[_-]?key)\\b"
                       "(\"?\\s*[:=]\\s*\"?)([^\\s\",}]+)"));
    out.replace(labelled, QStringLiteral("\\1\\2<redacted>"));

    // Email addresses.
    static const QRegularExpression email(
        QStringLiteral("\\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}\\b"));
    out.replace(email, QStringLiteral("<redacted-email>"));

    return out;
}

} // namespace orion
