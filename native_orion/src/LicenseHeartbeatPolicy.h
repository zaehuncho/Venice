#pragma once

// [CL2-P8-002 2026-09-23] Pure policy for the licence heartbeat's recovery path
// (docs/redteam/2026-09-22-launch/P8-real-world-robustness.md).
//
//   * LicenseHeartbeatBackoff - when the NEXT /api/license/check goes out. A
//     transport failure (or a non-kill structured error such as rate_limited)
//     retries at 15 s, 30 s, 60 s, then falls back to the normal 5-min cadence.
//     Any server answer that is not a failure resets the schedule.
//   * immediateHeartbeatAllowed - the debounce for event-driven heartbeats
//     (resume from sleep, network back online, lease just went stale). Windows
//     delivers several resume messages per wake and to every top-level window.
//   * leaseNoticeKind / leaseNoticeText - the customer copy shown while the fire
//     lease blocks automation for an authenticated user.
//
// Security: none of this touches LeaseGate. Retries only REQUEST a refresh; the
// lease is extended exclusively by a successful server heartbeat, exactly as
// before. Worst-case request rate is 4 per ~2 min + 1 per 10 s of events,
// far below the backend cap (RATELIMIT_MAX 30 per key per 60 s).
//
// No Qt event loop, no timers, no clocks: tests drive every input directly.
// Round 2 adds LicenseHeartbeatCoordinator (the whole decision state machine,
// including the in-flight re-run) and the engineering-log line builders.

#include <QtCore/QString>
#include <QtCore/QtGlobal>

namespace orion {

class LicenseHeartbeatBackoff final {
public:
    static constexpr int kNormalIntervalMs = 5 * 60 * 1000;
    // Retry ladder after consecutive failures 1, 2, 3; failure 4+ -> normal cadence.
    static constexpr int kRetryLadderMs[3] = {15'000, 30'000, 60'000};
    static constexpr int kRetryLadderSteps = 3;

    // A server answer that is not a failure (ok, or a kill verdict the caller
    // handles by stopping the timer): back to the normal cadence.
    void recordSuccess() noexcept { consecutiveFailures_ = 0; }

    // Transport failure, pin failure, invalid lease signature, or a structured
    // non-kill error. `rateLimited` (server said 429 rate_limited) skips the
    // short rungs straight to 60 s so a throttled client never hammers.
    void recordFailure(bool rateLimited = false) noexcept
    {
        if (rateLimited && consecutiveFailures_ < kRetryLadderSteps) {
            consecutiveFailures_ = kRetryLadderSteps;   // next delay = 60 s
            return;
        }
        if (consecutiveFailures_ < 1000) {
            ++consecutiveFailures_;
        }
    }

    [[nodiscard]] int consecutiveFailures() const noexcept { return consecutiveFailures_; }

    // Delay until the next heartbeat given the failures recorded so far.
    [[nodiscard]] int nextDelayMs() const noexcept
    {
        if (consecutiveFailures_ <= 0) {
            return kNormalIntervalMs;
        }
        if (consecutiveFailures_ <= kRetryLadderSteps) {
            return kRetryLadderMs[consecutiveFailures_ - 1];
        }
        return kNormalIntervalMs;
    }

    void reset() noexcept { consecutiveFailures_ = 0; }

private:
    int consecutiveFailures_ = 0;
};

// Minimum spacing between two EVENT-driven heartbeats (resume / network online /
// lease went stale). Scheduled retries do not go through this gate.
inline constexpr qint64 kImmediateHeartbeatMinSpacingMs = 10'000;

// lastImmediateMonotonicMs < 0 == never triggered.
[[nodiscard]] inline bool immediateHeartbeatAllowed(qint64 lastImmediateMonotonicMs,
                                                    qint64 nowMonotonicMs) noexcept
{
    if (lastImmediateMonotonicMs < 0) {
        return true;
    }
    const qint64 elapsed = nowMonotonicMs - lastImmediateMonotonicMs;
    // A negative elapsed means the caller's clock is not monotonic; allow rather
    // than wedge the recovery path (the request itself is harmless).
    return elapsed < 0 || elapsed >= kImmediateHeartbeatMinSpacingMs;
}

enum class LeaseNoticeKind {
    None,          // lease fine, gate off, or not signed in (AuthGate owns that)
    Reconnecting,  // no fresh server heartbeat: offline, asleep, server unreachable
    ClockOff,      // the server just answered ok but the local clock says the lease is already over
};

// freshlyVerifiedInvalidLease is true ONLY when a just-verified heartbeat's
// signed expiry was already unusable at receipt. A previously valid lease can
// expire naturally during sleep; that is Reconnecting, not a broken PC clock.
[[nodiscard]] inline LeaseNoticeKind leaseNoticeKind(bool gateEnabled,
                                                     bool authenticated,
                                                     bool fireAllowed,
                                                     bool freshlyVerifiedInvalidLease) noexcept
{
    if (!gateEnabled || !authenticated || fireAllowed) {
        return LeaseNoticeKind::None;
    }
    return freshlyVerifiedInvalidLease ? LeaseNoticeKind::ClockOff : LeaseNoticeKind::Reconnecting;
}

[[nodiscard]] inline QString leaseNoticeText(LeaseNoticeKind kind)
{
    switch (kind) {
    case LeaseNoticeKind::Reconnecting:
        // [COPY-FIX 2026-09-23 CW-10/EA-26] keyless auth: "subscription", not "licence".
        return QStringLiteral("Reconnecting to Venice servers — shots paused until your subscription is confirmed. Usually a few seconds.");
    case LeaseNoticeKind::ClockOff:
        return QStringLiteral("Your PC clock is off. Turn on 'Set time automatically' in Windows Date & Time settings, then restart Venice.");
    case LeaseNoticeKind::None:
        break;
    }
    return QString();
}

// [CL2-P8-002 round 2 2026-09-23] One owner for every heartbeat decision so the
// controller only executes actions and tests can drive the whole state machine
// on a fake clock with a fake server.
//
// In-flight race (Codex round 1): LicenseClient::validate() is a no-op while a
// request is in flight. An event (resume / network online / lease lapsed) that
// lands during an in-flight request is therefore remembered as ONE pending
// re-run. If that in-flight request fails (it was probably sent before the
// network came back) the re-run goes out immediately and the stale failure is
// not counted against the ladder; if it succeeds or is a kill, nothing re-runs.
// Bounded: at most one pending re-run, set only by a debounced event, consumed
// by the next completion; rate_limited never re-runs.
enum class HeartbeatOutcome {
    Ok,           // verified ok answer (lease refreshed by the caller)
    Failure,      // transport / pin / invalid lease signature / non-kill server code
    RateLimited,  // server 429 rate_limited
    Kill,         // isLicenseKillCode: the caller signs out; heartbeats stop
};

struct HeartbeatAction {
    bool sendNow = false;        // call LicenseClient::validate now
    bool stopTimer = false;      // stop the periodic heartbeat timer
    int restartTimerMs = -1;     // >= 0: (re)start the heartbeat timer with this interval
};

class LicenseHeartbeatCoordinator final {
public:
    // Activation succeeded: clean ladder, normal cadence, no remembered server ok.
    HeartbeatAction onSessionStarted() noexcept
    {
        backoff_.reset();
        lastServerOk_ = false;
        clockOffAtReceipt_ = false;
        pendingRerun_ = false;
        HeartbeatAction a;
        a.restartTimerMs = LicenseHeartbeatBackoff::kNormalIntervalMs;
        return a;
    }

    // Event-driven request (resume, network online, lease lapsed).
    HeartbeatAction onImmediateRequest(qint64 nowMonotonicMs, bool requestInFlight) noexcept
    {
        HeartbeatAction a;
        if (!immediateHeartbeatAllowed(lastImmediateMs_, nowMonotonicMs)) {
            return a;   // debounced
        }
        lastImmediateMs_ = nowMonotonicMs;
        backoff_.reset();   // a wake / network return restarts the short ladder
        if (requestInFlight) {
            pendingRerun_ = true;   // re-run once when the in-flight one completes
            return a;
        }
        a.sendNow = true;
        return a;
    }

    // A /api/license/check completed.
    HeartbeatAction onResult(HeartbeatOutcome outcome,
                             bool leaseValidAtReceipt = true) noexcept
    {
        HeartbeatAction a;
        const bool rerun = pendingRerun_;
        pendingRerun_ = false;
        switch (outcome) {
        case HeartbeatOutcome::Ok:
            lastServerOk_ = true;
            clockOffAtReceipt_ = !leaseValidAtReceipt;
            backoff_.recordSuccess();
            a.restartTimerMs = backoff_.nextDelayMs();
            return a;
        case HeartbeatOutcome::Kill:
            lastServerOk_ = false;
            clockOffAtReceipt_ = false;
            backoff_.reset();
            a.stopTimer = true;
            return a;
        case HeartbeatOutcome::Failure:
            lastServerOk_ = false;
            clockOffAtReceipt_ = false;
            if (rerun) {
                // Stale in-flight failure: send the event's request now and leave
                // the ladder where the event put it (reset). Its own result
                // schedules the next attempt.
                a.sendNow = true;
                a.restartTimerMs = LicenseHeartbeatBackoff::kRetryLadderMs[0];   // safety net if the send no-ops
                return a;
            }
            backoff_.recordFailure(false);
            a.restartTimerMs = backoff_.nextDelayMs();
            return a;
        case HeartbeatOutcome::RateLimited:
            lastServerOk_ = false;
            clockOffAtReceipt_ = false;
            backoff_.recordFailure(true);   // never re-run into a 429
            a.restartTimerMs = backoff_.nextDelayMs();
            return a;
        }
        return a;
    }

    [[nodiscard]] bool lastHeartbeatServerOk() const noexcept { return lastServerOk_; }
    [[nodiscard]] bool clockOffAtReceipt() const noexcept { return clockOffAtReceipt_; }
    [[nodiscard]] bool rerunPending() const noexcept { return pendingRerun_; }
    [[nodiscard]] int consecutiveFailures() const noexcept { return backoff_.consecutiveFailures(); }
    [[nodiscard]] int nextDelayMs() const noexcept { return backoff_.nextDelayMs(); }

private:
    LicenseHeartbeatBackoff backoff_;
    qint64 lastImmediateMs_ = -1;
    bool lastServerOk_ = false;
    bool clockOffAtReceipt_ = false;
    bool pendingRerun_ = false;
};

// [CL2-P8-002 round 2 2026-09-23] Engineering-log lines for the retry path. They
// deliberately start with "Lease heartbeat" and never contain "license"/"licence"
// so the customer Activity ring (ui_notifications::shouldEnterActivityRing,
// which allow-lists those words) does not show a Wi-Fi blip as a stream of
// retries; "lease heartbeat" is on that policy's deny-list. The customer sees
// only the banner transition lines (leaseNoticeText / leaseRestoredLogLine).
[[nodiscard]] inline QString heartbeatRetryLogLine(const QString& code, int nextDelayMs,
                                                   const QString& leaseState)
{
    return QStringLiteral("Lease heartbeat: attempt failed (%1); next attempt in %2 s; %3")
        .arg(code.isEmpty() ? QStringLiteral("no server response") : code)
        .arg(nextDelayMs / 1000)
        .arg(leaseState);
}

[[nodiscard]] inline QString heartbeatImmediateLogLine(const QString& reason, bool deferredBehindInFlight,
                                                       const QString& leaseState)
{
    return QStringLiteral("Lease heartbeat: immediate re-check (%1)%2; %3")
        .arg(reason,
             deferredBehindInFlight ? QStringLiteral(", queued behind the request in flight")
                                    : QString(),
             leaseState);
}

[[nodiscard]] inline QString heartbeatRecoveredLogLine(int failedAttempts, const QString& leaseState)
{
    return QStringLiteral("Lease heartbeat: recovered after %1 failed attempt(s); %2")
        .arg(failedAttempts)
        .arg(leaseState);
}

// Customer-visible: one line per banner transition. [COPY-FIX 2026-09-23] Says
// "subscription" (keyless auth); it reaches the Activity ring through the policy's
// default-keep rule (no lease/sidecar/counter shape), pinned in
// LicenseHeartbeatPolicyTests (shouldEnterActivityRing(leaseRestoredLogLine())).
[[nodiscard]] inline QString leaseRestoredLogLine()
{
    return QStringLiteral("Subscription confirmed: shots re-enabled.");
}

// [P-H 2026-09-23] Remembered sign-in: what the controller does after an activation
// that was started from the stored key FAILED. `definitiveRefusal` is
// isRememberedSignInRefusalCode(result.error) (LicenseClient.h). A refusal deletes the
// stored key and stops retrying; anything else keeps it and schedules the next
// re-submit on the heartbeat ladder (15 s / 30 s / 60 s, then the 5-min cadence;
// rate_limited jumps to 60 s). Retrying only re-asks the server - it never unlocks.
struct RememberedSignInDecision {
    bool clearStoredKey = false;
    int retryDelayMs = -1;   // >= 0: start the retry timer with this delay
};

[[nodiscard]] inline RememberedSignInDecision rememberedSignInAfterFailure(
    bool definitiveRefusal, bool rateLimited, LicenseHeartbeatBackoff& backoff) noexcept
{
    RememberedSignInDecision d;
    if (definitiveRefusal) {
        backoff.reset();
        d.clearStoredKey = true;
        return d;
    }
    backoff.recordFailure(rateLimited);
    d.retryDelayMs = backoff.nextDelayMs();
    return d;
}

} // namespace orion
