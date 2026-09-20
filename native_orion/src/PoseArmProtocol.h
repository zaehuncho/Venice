#pragma once

#include <QtCore/QJsonObject>
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

// Fill measurements cross the same JSON boundary as pose tokens.  A phase
// anchor is interpolated from two frames, so a coarse/sub-pixel transition (or
// a sub-pixel re-latch) must be represented explicitly and compared before the
// pair is joined.  Missing/malformed provenance is intentionally invalid: old
// sidecars continue to provide display/sampler telemetry, while phase timing
// stands down rather than guessing that two numerical rulers are compatible.
struct MeterFillEstimatorIdentity {
    QString mode;
    quint64 generation = 0;

    [[nodiscard]] bool isValid() const noexcept
    {
        return generation != 0
            && (mode == QLatin1String("coarse") || mode == QLatin1String("subpixel"));
    }
};

[[nodiscard]] inline MeterFillEstimatorIdentity decodeMeterFillEstimatorIdentity(
    const QJsonObject& payload)
{
    MeterFillEstimatorIdentity identity;
    const QString mode = payload.value(QStringLiteral("fill_estimator_mode"))
                             .toString().trimmed().toLower();
    quint64 generation = 0;
    if ((mode != QLatin1String("coarse") && mode != QLatin1String("subpixel"))
        || !decodePoseArmToken(
            payload.value(QStringLiteral("fill_estimator_generation")), &generation)) {
        return identity;
    }
    identity.mode = mode;
    identity.generation = generation;
    return identity;
}

} // namespace orion
