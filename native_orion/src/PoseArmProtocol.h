#pragma once

#include <QtCore/QJsonValue>
#include <QtCore/QString>
#include <QtCore/QtGlobal>

namespace orion {

// Pose arm tokens cross a JSON boundary. Keep them as canonical decimal strings
// so a 64-bit token is never rounded through JSON's IEEE-754 number storage.
[[nodiscard]] inline QString encodePoseArmToken(quint64 token)
{
    return token == 0 ? QString{} : QString::number(token);
}

[[nodiscard]] inline bool decodePoseArmToken(const QJsonValue& value, quint64* tokenOut)
{
    if (!tokenOut) {
        return false;
    }
    *tokenOut = 0;
    if (!value.isString()) {
        return false;
    }

    const QString encoded = value.toString();
    if (encoded.isEmpty() || encoded.size() > 20
        || encoded.front() < QLatin1Char('1') || encoded.front() > QLatin1Char('9')) {
        return false;
    }
    for (const QChar ch : encoded) {
        if (ch < QLatin1Char('0') || ch > QLatin1Char('9')) {
            return false;
        }
    }

    bool ok = false;
    const quint64 token = encoded.toULongLong(&ok, 10);
    if (!ok || token == 0 || QString::number(token) != encoded) {
        return false;
    }
    *tokenOut = token;
    return true;
}

} // namespace orion
