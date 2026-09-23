#pragma once

#include <QtCore/QFile>
#include <QtCore/QIODevice>
#include <QtCore/QRegularExpression>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QtGlobal>

#include <cmath>

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
        // Seen in the customer feed on the 2026-09-14 relaunch: preview transport plumbing and
        // the (shelved) meter-delay service handshake are machine state, not customer events.
        QStringLiteral("shm preview open pending"),
        QStringLiteral("shm preview reader opened"),
        QStringLiteral("meter delay service state:"),
        QStringLiteral("sidecar shipped timing profile:"),
    };
    for (const QString& marker : markers) {
        if (message.contains(marker, Qt::CaseInsensitive)) {
            return true;
        }
    }
    return false;
}

// ═══════════════════════════════════════════════════════════════════════════
// [ORION_ACTIVITY_FEED 2026-09-14 owner "overall polish"] THE ring rule.
//
// One hour of logs/orion_native.log grew 12,000 lines, and the customer-facing
// Activity panel showed all of it: 1105 `Sidecar: … ERROR simple_reader:
// DETECTOR HEALTH …`, 589 `Input hook heartbeat:`, 470 network-bridge repeats,
// 445 `preview_stats`, 445 `SHM preview frame read`, 439 `Telemetry stage
// split`, 438 + 438 preview-pipeline lines, 359 `Capture health`, 288
// `IDLE-GATE`, 88 `Release submit`, 72 `Self-grade diagnostic`. None of that is
// a customer event, and together they aged every real one out of the ring.
//
// This is the ONE place the split is decided. Everything classified as
// engineering still reaches logs/orion_native.log byte-for-byte (diagnostics and
// the offline tooling that parses those lines depend on it) — it simply does not
// enter the customer Activity ring.
//
// Precedence, in order:
//   0. INTERNAL-ONLY — lease / sidecar / ORION_ env lines never reach customers,
//                    even when they also carry an allow-listed word.
//   1. ALLOW-LIST — an explicitly customer-meaningful template wins outright,
//                    even when it carries engineering-shaped detail.
//   2. DENY-LIST   — the known telemetry templates.
//   3. `Sidecar:`  — raw sidecar stdout is a machine stream; the human events it
//                    implies arrive through the allow-listed templates instead.
//   4. COUNTER-LINE HEURISTIC — three or more `key=value` pairs is what a
//                    periodic counter line looks like, whatever it is called.
//                    This is the generalisation that catches the NEXT telemetry
//                    template nobody remembered to deny-list.
//   5. DEFAULT KEEP — fail OPEN. A new human-facing event must never be silently
//                    swallowed just because nobody added it to a list.
// ---------------------------------------------------------------------------

// Rule 0. [COPY-FIX 2026-09-23 NEW-A6] Internal-only lines. Checked BEFORE the
// allow-list, because allow-listed words ("watchdog", "safe mode", "stream warm-up
// hiccup", "controller") used to carry sidecar / lease / env-var text straight into
// the customer feed. These lines still reach logs/orion_native.log byte-for-byte.
// Markers are deliberately specific: a bare "lease" would also match "release".
[[nodiscard]] inline bool isInternalOnlyLine(const QString& message)
{
    static const QStringList internal = {
        QStringLiteral("lease-gated fire"),     // "Lease-gated fire ENABLED (ORION_LEASE_GATED_FIRE)…"
        QStringLiteral("fire lease"),           // "Fire lease seeded by activation: …"
        QStringLiteral("sidecar"),              // any process-level detection-engine detail
        QStringLiteral("orion_"),               // developer env-var names
        QStringLiteral("engine detail:"),       // raw Remote Play status behind a mapped one
        // The raw abort enum line (its reason can contain the allow-listed word
        // "controller"); the customer gets the plain "Shot not taken" line written
        // next to it (customerShotNotTakenText).
        QStringLiteral("shot automation aborted:"),
    };
    for (const QString& marker : internal) {
        if (message.contains(marker, Qt::CaseInsensitive)) {
            return true;
        }
    }
    return false;
}

// [COPY-FIX 2026-09-23 NEW-A5 / EA-25] Customer copy for a raw RemotePlaySession
// status. The Live Capture subtitle, the input-dead overlay and the Activity feed
// show THIS; the raw text goes to the engineering log ("Remote Play engine detail:",
// hidden by rule 0). Coded in the RP-01..RP-04 style RemotePlaySession already uses.
// Anything not recognised and free of engine words passes through unchanged (plain
// statuses like "Disconnected", "Waking the console from rest mode..." and the
// existing RP-0x copy).
[[nodiscard]] inline QString customerRemoteStatus(const QString& raw)
{
    const auto has = [&raw](const char* needle) {
        return raw.contains(QLatin1String(needle), Qt::CaseInsensitive);
    };
    if (has("Production package is incomplete")) {
        return QStringLiteral("Part of Venice is missing from this install (code RP-05). "
                              "Reinstall Venice from the latest download.");
    }
    if (has("Failed to launch autogreen sidecar")
        || has("sidecar script or Python runtime not found")
        || has("Compiled sidecar")) {
        return QStringLiteral("Venice's detection engine didn't start (code RP-06). "
                              "Restart Venice; if it happens again, reinstall from the latest download.");
    }
    if (has("Autogreen sidecar CRASHED") || has("sidecar died")) {
        return QStringLiteral("Venice's detection engine stopped (code RP-06). "
                              "If the picture doesn't come back, disconnect and connect again.");
    }
    if (has("Stream start did not confirm") || has("input session was not proven ready")) {
        return QStringLiteral("Venice couldn't link your controller to the PS5 (code RP-08). "
                              "Disconnect and connect again.");
    }
    if (has("Chiaki input recovery") || has("Recovered input child")) {
        return QStringLiteral("Venice couldn't reconnect your controller (code RP-07). Press Connect.");
    }
    if (has("Console IP is required")) {
        return QStringLiteral("Enter your PS5's IP address in Setup, then press Connect.");
    }
    // Progress lines.
    if (has("Starting Chiaki autogreen sidecar")) {
        return QStringLiteral("Starting\u2026");
    }
    if (has("Starting Chiaki input") || has("starting Chiaki input")) {
        return QStringLiteral("Connecting your controller\u2026");
    }
    if (has("Waiting for the detection sidecar")) {
        return QStringLiteral("Starting video detection\u2026");
    }
    if (has("Restarting detection sidecar")) {
        return QStringLiteral("Restarting video detection\u2026");
    }
    if (has("Recovering Chiaki input link") || has("Recovering console input session")
        || has("Chiaki input child relaunched")) {
        return QStringLiteral("Reconnecting your controller\u2026");
    }
    if (has("Autogreen running")) {
        return QStringLiteral("Connected \u2014 meter detection active");
    }
    if (raw.trimmed().compare(QLatin1String("Autogreen stopped"), Qt::CaseInsensitive) == 0) {
        return QStringLiteral("Stopped");
    }
    if (has("frame receiver boundary")) {
        return QStringLiteral("PS5 connection test passed");
    }
    // Catch-all: any other engine wording never reaches the customer verbatim.
    if (has("sidecar") || has("chiaki") || has("autogreen") || has("input child")
        || has("ORION_")) {
        return QStringLiteral("Venice hit an unexpected error (code RP-09). "
                              "Disconnect and connect again; if it repeats, restart Venice.");
    }
    return raw;
}

// [COPY-FIX 2026-09-23 NEW-A7] Shot Lead ms -> the 1..100 number the Shot Lead card
// shows. MUST mirror ShotLeadCard.qml valueFromMs (msLo 150, msHi 400).
[[nodiscard]] inline int shotLeadSliderValue(double ms)
{
    constexpr double kLo = 150.0;
    constexpr double kHi = 400.0;
    const long v = std::lround((ms - kLo) * 99.0 / (kHi - kLo)) + 1;
    return static_cast<int>(v < 1 ? 1 : (v > 100 ? 100 : v));
}

// [COPY-FIX 2026-09-23 NEW-A7] "Shot not taken (%1)." printed the raw AutomationEngine
// abort enum. Customers get a plain category; the enum stays in the engineering line
// "Shot automation aborted: <reason>" (tools/timing/live_batch_report.py parses it).
[[nodiscard]] inline QString customerShotNotTakenText(const QString& reason)
{
    const auto has = [&reason](const char* needle) {
        return reason.contains(QLatin1String(needle), Qt::CaseInsensitive);
    };
    if (has("cancel")) {
        return QStringLiteral("Shot canceled.");
    }
    if (has("lead_unready")) {
        return QStringLiteral("Shot not taken — Venice is still measuring your setup.");
    }
    if (has("deadline") || has("late") || has("timeout")) {
        return QStringLiteral("Shot not taken — Venice couldn't release in time.");
    }
    if (has("controller") || has("route") || has("delivery") || has("input_timer")
        || has("commit")) {
        return QStringLiteral("Shot not taken — the controller link wasn't ready.");
    }
    if (has("meter") || has("detector") || has("ownership") || has("structure") || has("pose")
        || has("candidate") || has("fill") || has("stale") || has("goto")) {
        return QStringLiteral("Shot not taken — Venice lost track of the shot.");
    }
    if (has("disarmed") || has("authority")) {
        return QStringLiteral("Shot not taken — automation was paused.");
    }
    return QStringLiteral("Shot not taken.");
}

// Rule 1. Customer-meaningful templates. Matched case-insensitively as
// substrings of the simplified line, so a prefix here covers every argument
// spelling of that template.
[[nodiscard]] inline bool isCustomerActivityTemplate(const QString& message)
{
    static const QStringList allow = {
        // Shots, in the plain language UserFacingReleaseTracker already writes.
        QStringLiteral("release command accepted"),
        QStringLiteral("shot release was not"),
        QStringLiteral("shot not taken"),
        QStringLiteral("shot canceled"),
        QStringLiteral("shot meter detected"),
        QStringLiteral("shot meter no longer visible"),
        // [ORION_BANNER_VERDICT_LIVE 2026-09-14] "Shot: EXCELLENT · WIDE OPEN" - one graded
        // shot read off the GAME'S OWN feedback banner. The most customer-meaningful line
        // the app writes, and allow-listed rather than left to rule 5 so no future
        // deny-list entry or counter heuristic can quietly swallow it. The trailing space
        // keeps it clear of the engineering "Shot state:" / "ShotTuning " templates.
        QStringLiteral("shot: "),
        // Connection / session state changes.
        QStringLiteral("remote play:"),
        QStringLiteral("auto-reconnect:"),
        QStringLiteral("connect ignored"),
        QStringLiteral("disconnect:"),
        QStringLiteral("feed paused:"),
        QStringLiteral("stream warm-up hiccup"),
        QStringLiteral("no console ip set"),
        QStringLiteral("consoles responded"),
        QStringLiteral("discovery "),
        // Capture / console problems.
        QStringLiteral("capture devices:"),
        QStringLiteral("capture recovered"),
        QStringLiteral("live capture preview:"),
        QStringLiteral("watchdog"),
        QStringLiteral("safe mode"),
        // Controller route / state.
        QStringLiteral("controller"),
        QStringLiteral("virtual pad"),
        QStringLiteral("physical pad"),
        QStringLiteral("xinput pads:"),
        // Licence, entitlement, operator notice.
        QStringLiteral("license"),
        QStringLiteral("licence"),
        QStringLiteral("motd"),
        QStringLiteral("entitlement"),
        QStringLiteral("activation "),
        QStringLiteral("legal agreement"),
        // First-run / setup / profile steps.
        QStringLiteral("first-run"),
        QStringLiteral("meter calibration"),
        QStringLiteral("venice profile"),
        QStringLiteral("profile '"),
        QStringLiteral("profile ->"),
        QStringLiteral("update gate:"),
    };
    for (const QString& marker : allow) {
        if (message.contains(marker, Qt::CaseInsensitive)) {
            return true;
        }
    }
    return false;
}

// Rule 2. Periodic machine telemetry BEYOND the per-frame set above: the
// counter/diagnostic templates that the 2026-09-14 census found dominating the
// customer ring. Deliberately a separate list from
// isPeriodicMachineDiagnostic(), which also decides what may dirty the QML model
// at frame cadence — these lines are low-rate enough to reach `logs_`, they are
// simply not customer events.
[[nodiscard]] inline bool isEngineeringTelemetryLine(const QString& message)
{
    if (isPeriodicMachineDiagnostic(message)) {
        return true;
    }
    static const QStringList deny = {
        QStringLiteral("detector health"),        // Sidecar: … DETECTOR HEALTH …
        QStringLiteral("telemetry stage split:"),
        QStringLiteral("idle-gate:"),
        QStringLiteral("self-grade diagnostic"),
        QStringLiteral("session grade"),
        QStringLiteral("release issued:"),
        QStringLiteral("release submit:"),
        QStringLiteral("release attribution:"),
        QStringLiteral("release timing:"),
        QStringLiteral("release detsummary:"),
        QStringLiteral("release freshness:"),
        QStringLiteral("release vision:"),
        QStringLiteral("release tempo:"),
        QStringLiteral("release tick:"),
        QStringLiteral("release ownership:"),
        QStringLiteral("release-window diagnostic"),
        QStringLiteral("shadow timing:"),
        QStringLiteral("scheduled fire:"),
        QStringLiteral("shot state:"),
        QStringLiteral("shottuning "),
        QStringLiteral("route decision:"),
        QStringLiteral("embed regrab:"),
        QStringLiteral("meter delay condition:"),
        // Diagnostics-only network plumbing. It is never timing authority and a
        // customer can do nothing with it; the shelved bridge's repeats are what
        // produced 470 identical "connected - Access is denied" lines in an hour.
        QStringLiteral("network bridge:"),
        QStringLiteral("packet bridge"),
        QStringLiteral("winmm raw"),
        // [CL2-P8-002 round 2 2026-09-23] Heartbeat retry / re-check chatter
        // (LicenseHeartbeatPolicy.h builders). The customer gets the banner and one
        // transition line instead of a line per retry during a Wi-Fi blip.
        QStringLiteral("lease heartbeat"),
    };
    for (const QString& marker : deny) {
        if (message.contains(marker, Qt::CaseInsensitive)) {
            return true;
        }
    }
    return false;
}

// Rule 4. A periodic counter line's real signature: a run of `key=value` tokens.
// Three is the threshold — real prose ("Shot Lead: video source A -> B, lead
// 1 -> 2") never reaches it, while every telemetry template in the census does.
[[nodiscard]] inline bool looksLikeCounterLine(const QString& message)
{
    static const QRegularExpression pair(QStringLiteral(
        "(?:^|\\s)[A-Za-z_][A-Za-z0-9_.\\[\\]]*=[^\\s]"));
    int pairs = 0;
    QRegularExpressionMatchIterator matches = pair.globalMatch(message);
    while (matches.hasNext()) {
        matches.next();
        if (++pairs >= 3) {
            return true;
        }
    }
    return false;
}

// THE rule. True == this line belongs in the customer Activity ring.
[[nodiscard]] inline bool shouldEnterActivityRing(const QString& message)
{
    const QString line = message.trimmed();
    if (line.isEmpty()) {
        return false;
    }
    if (isInternalOnlyLine(line)) {
        return false;                                  // 0
    }
    if (isCustomerActivityTemplate(line)) {
        return true;                                   // 1
    }
    if (isEngineeringTelemetryLine(line)) {
        return false;                                  // 2
    }
    if (line.startsWith(QLatin1String("Sidecar:"), Qt::CaseInsensitive)) {
        return false;                                  // 3
    }
    if (looksLikeCounterLine(line)) {
        return false;                                  // 4
    }
    return true;                                       // 5
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
    // shape. Keep this defence-in-depth redaction in production too: a user can
    // still paste an arbitrary key-shaped value into the UI even though the
    // production authenticator cannot accept a local-development entitlement.
    // The character-class spelling deliberately avoids embedding the exact dev
    // auth marker in a production binary; the strict release audit reserves that
    // literal for detecting accidentally compiled authentication hooks.
    static const QRegularExpression devKeyShape(
        QStringLiteral("(N[V]DEV)-[A-Za-z0-9-]{2,}"));
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
    line.replace(devKeyShape, QStringLiteral("\\1-\u2022\u2022\u2022\u2022"));
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

// A successful drain is not enough when admission dropped a prior batch. In
// either case the ring is the newest shareable source; do not let a nonempty
// older disk tail hide the error that prompted Copy Log.
[[nodiscard]] inline QString activityLogCopyForSharing(
    const QString& redactedDiskTail, const QStringList& ring,
    bool drained, bool storageFault, quint64 droppedBatches)
{
    const QString redactedRing = serializeActivityLogForSharing(ring);
    if (!drained || storageFault || droppedBatches != 0) {
        return QStringLiteral("Disk log incomplete; recent session events follow.\n")
            + redactedRing;
    }
    return redactedDiskTail.isEmpty() ? redactedRing : redactedDiskTail;
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
