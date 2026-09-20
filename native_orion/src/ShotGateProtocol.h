#pragma once

#include "PoseArmProtocol.h"

#include <QtCore/QJsonObject>
#include <QtCore/QLatin1Char>
#include <QtCore/QString>

namespace orion {

// [ORION_SHOT_GATE_TYPE 2026-09-15] Construction of the native -> sidecar SHOT GATE commands,
// kept pure (no QProcess, no session state) so the wire format is unit-testable.
//
// WHY THE TYPE TRAVELS AT ALL. player_anchor.onset_window_ms() bounds WHEN the meter's onset is
// due after the press. Without a shot type it can only use the UNION of the measured onsets
// (~150 ms standing, ~675 ms on a fade) -- a 1.05 s slice. The engine has already classified the
// press on the very poll that carried the Square edge, so shipping that one string with the arm
// narrows the window to ~0.5 s, which is what makes the locator's relaxed sub-floor acquire
// affordable.
//
// THE CONTRACT IS ADDITIVE IN BOTH DIRECTIONS.
//   * An OLD sidecar + a NEW engine: the extra `shot_type`/`rhythm` keys on shot_gate_arm are
//     ignored (its handler reads only `source`/`shot_epoch`), and shot_gate_release /
//     shot_gate_disarm fall off the end of the stdin if/elif chain, which has no else. That is
//     exactly today's behaviour: the anchor window closes on ORION_ANCHOR_ARM_S.
//   * A NEW sidecar + an OLD engine: no `shot_type` arrives, so the union window stands; no
//     release/disarm marker arrives, so the window again closes on ORION_ANCHOR_ARM_S.
// Neither side may ever require the other to be new.
//
// Epochs travel as canonical decimal STRINGS (encodePoseArmToken), for the same reason pose arm
// tokens do: a 64-bit epoch must never round through JSON's IEEE-754 number storage.

// The engine classifier's own labels ("Standstill", "Left Fade", "Right Fade", "No Dip",
// "Post Fade", "Go-To"). Trimmed and bounded so a malformed value can neither grow the command
// nor smuggle whitespace into the receipt line the sidecar prints.
[[nodiscard]] inline QString encodeShotGateShotType(const QString& shotType)
{
    return shotType.trimmed().left(24);
}

// Abort/cancel reasons are single snake_case tokens on every other Orion log line; keep that
// invariant on the wire so the sidecar's receipt stays one whitespace-free field.
[[nodiscard]] inline QString encodeShotGateReason(const QString& reason)
{
    QString value = reason.trimmed().left(32);
    value.replace(QLatin1Char(' '), QLatin1Char('_'));
    return value;
}

// Render a type for a LOG line (never for the wire): the abort-identity convention, so one
// parser reads `shot_type=` on the arm-send line, the abort line and the SHOT NOT OWNED census.
[[nodiscard]] inline QString shotGateShotTypeField(const QString& shotType)
{
    const QString encoded = encodeShotGateShotType(shotType);
    if (encoded.isEmpty()) {
        return QStringLiteral("unclassified");
    }
    QString value = encoded;
    value.replace(QLatin1Char(' '), QLatin1Char('_'));
    return value;
}

// {"cmd":"shot_gate_arm","source":"square_edge","shot_epoch":"41","shot_type":"Left Fade",
//  "rhythm":0}
// `source` also carries the UPDATE case: the 200 ms blind type grace re-sends the SAME epoch
// with source="type_upgrade", which the sidecar recognises as a duplicate epoch and therefore
// refreshes the type on WITHOUT moving the press timestamp.
[[nodiscard]] inline QJsonObject makeShotGateArmCommand(
    const QString& source, quint64 physicalShotEpoch,
    const QString& shotType = QString(), bool rhythm = false)
{
    QJsonObject command{
        {QStringLiteral("cmd"), QStringLiteral("shot_gate_arm")},
        {QStringLiteral("source"), source},
        {QStringLiteral("shot_epoch"), encodePoseArmToken(physicalShotEpoch)},
    };
    // OMITTED rather than sent empty: "absent" and "" must mean the same thing to the sidecar
    // (keep the union window), and only one of the two spellings should exist on the wire.
    const QString encodedType = encodeShotGateShotType(shotType);
    if (!encodedType.isEmpty()) {
        command.insert(QStringLiteral("shot_type"), encodedType);
    }
    command.insert(QStringLiteral("rhythm"), rhythm ? 1 : 0);
    return command;
}

// {"cmd":"shot_gate_release","shot_epoch":"41","release_ms":1757913600123.4}
// release_ms is EPOCH ms (the sidecar's own time.time()*1000 fill-sample clock, sub-ms like the
// release marker's wall_ms), so the sidecar can place the release on the same timeline as its
// frames instead of inferring it.
[[nodiscard]] inline QJsonObject makeShotGateReleaseCommand(
    quint64 physicalShotEpoch, double releaseWallMsEpoch)
{
    return QJsonObject{
        {QStringLiteral("cmd"), QStringLiteral("shot_gate_release")},
        {QStringLiteral("shot_epoch"), encodePoseArmToken(physicalShotEpoch)},
        {QStringLiteral("release_ms"), releaseWallMsEpoch},
    };
}

// {"cmd":"shot_gate_disarm","shot_epoch":"41","reason":"square_early_release"}
// The press ended with NO bot release: the player let go (manual cancel / pump fake) or the
// engine aborted. Same effect on the reader as a release -- the press window closes now.
[[nodiscard]] inline QJsonObject makeShotGateDisarmCommand(
    quint64 physicalShotEpoch, const QString& reason)
{
    return QJsonObject{
        {QStringLiteral("cmd"), QStringLiteral("shot_gate_disarm")},
        {QStringLiteral("shot_epoch"), encodePoseArmToken(physicalShotEpoch)},
        {QStringLiteral("reason"), encodeShotGateReason(reason)},
    };
}

} // namespace orion
