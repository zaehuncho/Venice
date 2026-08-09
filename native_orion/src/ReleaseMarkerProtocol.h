#pragma once

#include "PoseArmProtocol.h"

#include <QtCore/QJsonObject>

namespace orion {

// Keep construction of the native -> sidecar release-marker command pure and
// testable.  `calibration` is additive: older sidecars ignore it, while newer
// sidecars default a missing field to false for older native clients.
[[nodiscard]] inline QJsonObject makeReleaseMarkerCommand(
    int seq, double wallMs, bool calibration = false,
    double validationTargetPct = -1.0,
    double validationTolerancePct = -1.0,
    quint64 physicalShotEpoch = 0,
    quint64 shotAttempt = 0)
{
    QJsonObject command{
        {QStringLiteral("cmd"), QStringLiteral("release_marker")},
        {QStringLiteral("seq"), seq},
        {QStringLiteral("wall_ms"), wallMs},
        {QStringLiteral("calibration"), calibration},
    };
    if (calibration && validationTargetPct > 0.0
        && validationTolerancePct > 0.0) {
        command.insert(QStringLiteral("validation_target_pct"), validationTargetPct);
        command.insert(QStringLiteral("validation_tolerance_pct"), validationTolerancePct);
    }
    const QString encodedPhysicalEpoch = encodePoseArmToken(physicalShotEpoch);
    const QString encodedShotAttempt = encodePoseArmToken(shotAttempt);
    if (!encodedPhysicalEpoch.isEmpty() && !encodedShotAttempt.isEmpty()) {
        command.insert(QStringLiteral("physical_epoch"), encodedPhysicalEpoch);
        command.insert(QStringLiteral("shot_attempt"), encodedShotAttempt);
    }
    return command;
}

} // namespace orion
