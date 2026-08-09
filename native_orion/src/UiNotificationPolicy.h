#pragma once

#include <QtCore/QFile>
#include <QtCore/QIODevice>
#include <QtCore/QRegularExpression>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QtGlobal>

namespace orion::ui_notifications {

// Broad statusChanged is the NOTIFY signal for a large portion of the QML
// surface. Frame/input telemetry keeps its dedicated signals; this human-scale
// cadence is only for the redundant broad notification.
inline constexpr qint64 kBroadStatusMinimumIntervalMs = 250;
// The compact live meter card benefits from perceptibly live values, but text
// formatting/binding work at detector cadence competes with the 60 Hz preview.
// Fifteen updates per second is smooth for numeric telemetry while leaving the
// raw 60 Hz samples untouched for AutomationEngine.
inline constexpr qint64 kLiveMeterMinimumIntervalMs = 67;

class StatusNotificationThrottle final {
public:
    explicit StatusNotificationThrottle(
        qint64 minimumIntervalMs = kBroadStatusMinimumIntervalMs) noexcept
        : minimumIntervalMs_(minimumIntervalMs > 0 ? minimumIntervalMs : 1)
    {
    }

    [[nodiscard]] bool take(qint64 nowMs) noexcept
    {
        if (lastNotificationMs_ < 0 || nowMs < lastNotificationMs_
            || nowMs - lastNotificationMs_ >= minimumIntervalMs_) {
            lastNotificationMs_ = nowMs;
            return true;
        }
        return false;
    }

    void markImmediate(qint64 nowMs) noexcept { lastNotificationMs_ = nowMs; }
    [[nodiscard]] qint64 lastNotificationMs() const noexcept {
        return lastNotificationMs_;
    }

private:
    qint64 minimumIntervalMs_;
    qint64 lastNotificationMs_ = -1;
};

// Detector publication and the following controller tick are deliberately two
// separate events. Sharing one throttle let the detector's fill notification at
// t0 suppress the first real target-matched ETA at t0+~4 ms indefinitely. Keep
// independent 15 Hz budgets so both truthful values can cross unavailable ->
// available without increasing either event's steady-state UI cadence.
class LiveMeterNotificationCadence final {
public:
    [[nodiscard]] bool takeSample(qint64 nowMs) noexcept
    {
        return sample_.take(nowMs);
    }

    [[nodiscard]] bool takeEta(qint64 nowMs) noexcept
    {
        return eta_.take(nowMs);
    }

private:
    StatusNotificationThrottle sample_{kLiveMeterMinimumIntervalMs};
    StatusNotificationThrottle eta_{kLiveMeterMinimumIntervalMs};
};

// These messages are periodic machine diagnostics. They remain byte-for-byte
// in the native disk log, but never enter or dirty the bounded user-facing
// Activity ring; otherwise a few minutes of hidden telemetry ages out every
// useful connection/shot event.
[[nodiscard]] inline bool isPeriodicMachineDiagnostic(const QString& message)
{
    static const QStringList markers = {
        QStringLiteral("qml_preview_pipeline:"),
        QStringLiteral("preview_pipeline:"),
        QStringLiteral("preview_stats:"),
        QStringLiteral("capture health:"),
        QStringLiteral("shm preview frame read:"),
        QStringLiteral("input hook heartbeat:"),
        // Detector presence can alternate between duplicate/rejected/accepted at
        // frame cadence while a shot is active.  It remains important disk
        // evidence, but rebuilding the activity pane for those transitions
        // competes with the live preview on the GUI thread.
        QStringLiteral("detection presence:"),
        QStringLiteral("detector frame rejected:"),
    };
    for (const QString& marker : markers) {
        if (message.contains(marker, Qt::CaseInsensitive)) {
            return true;
        }
    }
    return false;
}

// ---------------------------------------------------------------------------
// One-click "Copy all" support for the Activity view.
//
// The UI ring is DESIGNED to be shareable: identifiers enter it suffix-only
// (key_suffix=, machine_id_suffix=) and the full license key never crosses
// into QML (OrionAppController::copyLicenseKey / licenseKeyMasked). The copy
// now primarily serialises the on-disk ENGINEER stream though, which carries
// far more identifier-shaped material, so every copied line routes through
// this redaction pass — the destination is explicitly outside the app, a
// support ticket or an AI chat. Every mask keeps a short suffix so a
// redacted line can still be correlated with the complete on-disk log.

// Long opaque identifier runs: the 64-hex machineId()/digests and base64url
// backend tokens (token_urlsafe(32) = 43 chars). Calibrated against the real
// orion_native.log on the dev rig (2026-08-08): the class deliberately
// excludes '/', '+' and '=' (a base64-class mask ate filesystem paths and
// the "sha256=" label), and a run is masked only if it contains a digit —
// the engineer stream carries 40+ char digit-less snake_case enums
// ("reason=waiting_for_genuine_meter_before_ownership") that must survive,
// while random hex/base64url material without a single digit is vanishingly
// rare (< 0.1%). The kept 6-char suffix preserves correlation.
[[nodiscard]] inline QString maskOpaqueIdentifierRuns(const QString& line)
{
    static const QRegularExpression opaqueRun(QStringLiteral(
        "(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])"));
    QString out;
    qsizetype consumed = 0;
    QRegularExpressionMatchIterator matches = opaqueRun.globalMatch(line);
    while (matches.hasNext()) {
        const QRegularExpressionMatch match = matches.next();
        const QString run = match.captured(0);
        bool hasDigit = false;
        for (const QChar ch : run) {
            if (ch.isDigit()) {
                hasDigit = true;
                break;
            }
        }
        if (!hasDigit) {
            continue;   // long enum/prose, not an identifier
        }
        out.append(line.mid(consumed, match.capturedStart(0) - consumed));
        out.append(QStringLiteral("\u2022\u2022\u2022\u2022"));
        out.append(run.right(6));
        consumed = match.capturedEnd(0);
    }
    if (consumed == 0) {
        return line;   // nothing masked; common case, no reallocation
    }
    out.append(line.mid(consumed));
    return out;
}

[[nodiscard]] inline QString redactActivityLineForSharing(QString line)
{
    // Local native dev keys carry a distinctive prefix instead of the retail
    // shape; runs first so the retail rule below never sees its tail groups.
    static const QRegularExpression devKeyShape(
        QStringLiteral("NVDEV-[A-Za-z0-9-]{2,}"));
    // Retail license keys are 4 dash-joined groups of 4 (backend
    // gen_license_key). The lookarounds stop the rule biting inside longer
    // dash-joined runs (UUIDs, HID device paths). Mask style mirrors
    // licenseKeyMasked().
    static const QRegularExpression licenseKeyShape(QStringLiteral(
        "(?<![A-Za-z0-9-])[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){2}-"
        "([A-Za-z0-9]{4})(?![A-Za-z0-9-])"));
    // Escape sequences (not literal bullet characters): these sources carry
    // no BOM and the build sets no /utf-8, so raw multi-byte characters in a
    // header are at the mercy of the host codepage. Escapes are codepage-proof.
    line.replace(devKeyShape, QStringLiteral("NVDEV-\u2022\u2022\u2022\u2022"));
    line.replace(licenseKeyShape,
                 QStringLiteral("\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022-"
                                "\u2022\u2022\u2022\u2022-\\1"));
    return maskOpaqueIdentifierRuns(line);
}

// "Copy all" serialisation of the user-facing Activity ring: every retained
// line (not just the rows a page's filter shows), original order, one line
// per entry, after the sharing redaction pass above.
[[nodiscard]] inline QString serializeActivityLogForSharing(const QStringList& lines)
{
    QStringList out;
    out.reserve(lines.size());
    for (const QString& line : lines) {
        out.append(redactActivityLineForSharing(line));
    }
    return out.join(QLatin1Char('\n'));
}

// Cap on the user-facing Activity ring (`logs_` in OrionAppController).
// 2026-08-08: raised 160 -> 1000 so the owner can actually skim a whole
// batch in-app (the 160-line ring aged a session's setup out within
// minutes). Human, non-periodic lines only (periodic machine telemetry is
// filtered before the ring — see appendLog()), so 1000 lines is minutes to
// hours of real activity and still a trivial QStringList in memory. The
// on-disk logs/orion_native.log stream is unaffected by this cap.
inline constexpr qsizetype kActivityRingMaxLines = 1000;

// The share copy sources the TAIL of the on-disk engineer log, because the
// UI ring (kActivityRingMaxLines) is still shorter than the disk history and
// the failure being reported may have aged out of it.
//
// Bounds, calibrated against the real orion_native.log on the dev rig
// (2026-08-08: 10,684 lines, avg 211 bytes/line): 256 KiB is a defensible
// ceiling for pasting into a chat input and an AI context window, and buys
// ~1200 engineer lines — roughly three minutes of an ACTIVE shooting session
// (~8 lines/s hot) or hours of an idle one. 1500 lines caps the short-line
// pathological case so "how much history did I paste" stays predictable.
// Whichever bound is smaller wins, and the cut always lands on a line
// boundary. Anything older is reachable via openLogsFolder().
inline constexpr qint64 kActivityShareTailMaxBytes = 256 * 1024;
inline constexpr qsizetype kActivityShareTailMaxLines = 1500;

// Reads the bounded tail of the on-disk diagnostic log and serialises it for
// sharing (same redaction pass as the ring). Returns an empty string when the
// file is absent, locked, empty, or unreadable — the caller then falls back
// to the in-memory ring so the copy button never silently does nothing.
// Seeks from the end; never loads more than maxBytes (+ the QString decode).
[[nodiscard]] inline QString readLogTailForSharing(
    const QString& logPath,
    qint64 maxBytes = kActivityShareTailMaxBytes,
    qsizetype maxLines = kActivityShareTailMaxLines)
{
    if (maxBytes <= 0 || maxLines <= 0) {
        return {};
    }
    QFile file(logPath);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    const qint64 size = file.size();
    if (size <= 0) {
        return {};
    }
    const qint64 start = size > maxBytes ? size - maxBytes : 0;
    if (start > 0 && !file.seek(start)) {
        return {};
    }
    const QByteArray raw = file.read(size - start);
    if (raw.isEmpty()) {
        return {};
    }
    QString text = QString::fromUtf8(raw);
    text.replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
    QStringList lines = text.split(QLatin1Char('\n'));
    while (!lines.isEmpty() && lines.constLast().isEmpty()) {
        lines.removeLast();   // the file's trailing newline
    }
    if (start > 0 && !lines.isEmpty()) {
        // The byte cut landed mid-line (and possibly mid-UTF-8 sequence, which
        // fromUtf8 turned into replacement characters confined to this
        // fragment); drop it so every kept line is complete.
        lines.removeFirst();
    }
    if (lines.size() > maxLines) {
        lines = lines.mid(lines.size() - maxLines);
    }
    if (lines.isEmpty()) {
        return {};
    }
    return serializeActivityLogForSharing(lines);
}

// Presence states that may legitimately alternate at capture cadence without a
// user-visible state transition.  Suppress only noise-to-noise transitions;
// entering or leaving `accepted` is still recorded immediately in the disk log.
[[nodiscard]] inline bool isNoisyDetectionPresence(const QString& presence)
{
    return presence == QLatin1String("duplicate")
        || presence == QLatin1String("idle_overlay")
        || presence == QLatin1String("rejected")
        || presence == QLatin1String("stale");
}

} // namespace orion::ui_notifications
