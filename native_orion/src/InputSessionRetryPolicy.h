#pragma once

#include "ControllerRoutingPolicy.h"
#include "OrionTypes.h"

#include <QtCore/QtGlobal>

// [ORION_INPUT_DEAD_UX 2026-08-30] Pure policy for the two halves of the
// "player stranded on dead input" fix (owner invariant: "sometimes the bot
// won't shoot even when holding square. That must NOT happen"):
//
//   1. InputSessionRetryPlanner — bounded, backoff-spaced auto-retry of the
//      Chiaki INPUT session after a failed/dropped promotion or recovery.
//   2. inputDeadOverlaySeverity — the on-screen verdict for the unmissable
//      "input is NOT reaching the console" overlay, built on the SAME
//      pressUndeliverable() predicate the per-press forensic line uses.
//
// Everything here is deterministic and Qt-free beyond qint64, so the retry
// state machine and the overlay condition are unit-testable without the app.
//
// SAFETY ARGUMENT (why a retry can never double-spawn or fake readiness):
//   * A retry only ever calls the SAME user paths a manual Connect uses
//     (disconnectRemotePlay(true) + connectRemotePlay()). It never touches
//     session state directly, so the started/error verdict contract and the
//     launch-scoped streaminfo readiness proof remain the only way to Running.
//   * connectRemotePlay() refuses while Connecting/Running (re-entrancy guard),
//     the sidecar stdin command loop is single-threaded (start_stream commands
//     serialize), _launch_remote_play_client is idempotent and reaps a failed
//     manager, and the standby pool refuses to spawn beside any chiaki-family
//     process. Four independent layers against a duplicate client.
//   * The one hazard a WARM retry cannot rule out is a start_stream QUEUED
//     behind a promote that is still wedged inside the sidecar (the native
//     deadline fires while Python has produced no verdict). Executing later,
//     that queued command could bring up an input client the native has
//     already condemned — an orphan holding the pipe. Therefore every
//     no-verdict class (DeadlineTimeout, CommandUndeliverable) retries COLD:
//     synchronous teardown first, which kills the sidecar process (job object
//     reaps its children), then a fresh cold connect. Only SidecarVerdict —
//     where the verdict event itself proves the sidecar's command loop has
//     returned — retries warm (in-place re-promotion, Elgato/detector kept).
//   * IdentityBlocked (trusted client image unverifiable) never retries: a
//     security refusal must not be retried into, and it cannot heal by
//     looping.
//   * Rest-mode wake is respected, not fought: a retry is only ever scheduled
//     from a TERMINAL failure (never while Connecting, which is where the
//     sidecar's own wake budget lives), and a wake-observed failure gets extra
//     delay so the console can finish booting before the next attempt.
//
// Kill switch: ORION_INPUT_SESSION_AUTORETRY=0 (read by OrionAppController;
// the overlay stays on regardless — visibility must never be killable).

namespace orion {

// Wire values are stable (the cross-library signal carries the int).
enum class InputSessionFailureClass : int {
    // The sidecar delivered an explicit failed verdict (error event, or
    // started without input authority). Its command loop has provably
    // returned, so an in-place warm re-promotion is safe.
    SidecarVerdict = 0,
    // The native deadline fired with NO verdict — the sidecar may be wedged
    // inside the promotion. Retry must be COLD (teardown + fresh sidecar) so
    // a queued start_stream can never resurrect a condemned session.
    DeadlineTimeout = 1,
    // The command never reached the sidecar (process gone / write refused).
    // Cold retry rebuilds the sidecar.
    CommandUndeliverable = 2,
    // Trusted Remote Play client image unavailable/unverifiable. NEVER
    // auto-retried: security refusals are terminal until the user acts.
    IdentityBlocked = 3,
    // connectRemotePlay() itself refused before starting a session (pad gate,
    // security lock, missing IP). Nothing was launched; retry warm.
    LocalRefusal = 4,
};

struct InputSessionRetryDecision {
    bool retry = false;
    // Teardown (disconnectRemotePlay(true)) before the reconnect.
    bool coldRestart = false;
    int attempt = 0;      // 1-based; 0 when retry == false
    qint64 delayMs = 0;
};

class InputSessionRetryPlanner {
public:
    // Bounded: after this many automatic attempts the machine gives up cleanly
    // and the overlay tells the player to press Connect.
    static constexpr int kMaxAttempts = 4;
    // Backoff per attempt. A promotion itself can take up to 20 s (33+ s with
    // a rest-mode wake), so this can never look like a hot loop: worst case is
    // 4 attempts spread over ~49 s of timer delay plus each attempt's own
    // bounded connect budget.
    static constexpr qint64 kBackoffMs[kMaxAttempts] = {2000, 5000, 12000, 30000};
    // A failure that saw a rest-mode wake in progress waits longer: the
    // console may still be booting, and the wake path's own budget/message
    // must not be trampled by an eager reconnect.
    static constexpr qint64 kWakeExtraDelayMs = 8000;
    // Press-triggered retry throttle (see onUndeliverablePress).
    static constexpr qint64 kPressRetryThrottleMs = 60'000;
    // Fire slightly off the input-poll tick that observed the press.
    static constexpr qint64 kPressRetryDelayMs = 400;

    [[nodiscard]] InputSessionRetryDecision onFailure(
        InputSessionFailureClass cls, bool wakeObserved) noexcept
    {
        lastClass_ = cls;
        if (cls == InputSessionFailureClass::IdentityBlocked) {
            identityBlocked_ = true;
            exhausted_ = true;
            return {};
        }
        if (attemptsUsed_ >= kMaxAttempts) {
            exhausted_ = true;
            return {};
        }
        ++attemptsUsed_;
        InputSessionRetryDecision d;
        d.retry = true;
        d.attempt = attemptsUsed_;
        d.coldRestart = classNeedsColdRestart(cls);
        d.delayMs = kBackoffMs[attemptsUsed_ - 1]
            + (wakeObserved ? kWakeExtraDelayMs : 0);
        return d;
    }

    // The player pressing Square while input is dead is the strongest possible
    // "I expect to be connected" signal. Grant ONE extra attempt per throttle
    // window, but only once the scheduled budget is exhausted (while a backoff
    // retry is pending it owns recovery — presses must not spam reconnects),
    // and never over a security refusal.
    [[nodiscard]] InputSessionRetryDecision onUndeliverablePress(qint64 nowMs) noexcept
    {
        if (!exhausted_ || identityBlocked_) {
            return {};
        }
        if (lastPressRetryMs_ != 0
            && nowMs - lastPressRetryMs_ < kPressRetryThrottleMs) {
            return {};
        }
        lastPressRetryMs_ = nowMs;
        InputSessionRetryDecision d;
        d.retry = true;
        d.attempt = ++attemptsUsed_;
        d.coldRestart = classNeedsColdRestart(lastClass_);
        d.delayMs = kPressRetryDelayMs;
        return d;
    }

    // Session reached Running / user issued a fresh manual Connect or
    // Disconnect: the budget belongs to a session intent, so it resets.
    void reset() noexcept
    {
        attemptsUsed_ = 0;
        exhausted_ = false;
        identityBlocked_ = false;
        lastPressRetryMs_ = 0;
        lastClass_ = InputSessionFailureClass::SidecarVerdict;
    }

    [[nodiscard]] int attemptsUsed() const noexcept { return attemptsUsed_; }
    [[nodiscard]] bool exhausted() const noexcept { return exhausted_; }
    [[nodiscard]] bool identityBlocked() const noexcept { return identityBlocked_; }
    [[nodiscard]] InputSessionFailureClass lastClass() const noexcept { return lastClass_; }

    [[nodiscard]] static constexpr bool classNeedsColdRestart(
        InputSessionFailureClass cls) noexcept
    {
        return cls == InputSessionFailureClass::DeadlineTimeout
            || cls == InputSessionFailureClass::CommandUndeliverable;
    }

private:
    int attemptsUsed_ = 0;
    bool exhausted_ = false;
    bool identityBlocked_ = false;
    qint64 lastPressRetryMs_ = 0;
    InputSessionFailureClass lastClass_ = InputSessionFailureClass::SidecarVerdict;
};

// ---------------------------------------------------------------------------
// On-screen overlay verdict.
//
// The overlay exists for exactly one trap: VIDEO LOOKS ALIVE while INPUT IS
// DEAD. Its core condition is the SAME pressUndeliverable() the per-press
// forensic line calls — one source of truth. Two additional visibility gates
// keep it honest rather than noisy:
//   * videoAlive — with no live pixels there is no illusion to break (the
//     page's ordinary Disconnected/Error state already owns that surface);
//   * sessionIntentActive OR a recent undeliverable press — the pre-Connect
//     browse (warm preview, user never pressed Connect) must not scream; but
//     the player PRESSING SQUARE in that state proves they believe they are
//     connected, so the press latches the overlay on for a few seconds.
// ---------------------------------------------------------------------------

enum class InputDeadSeverity {
    None = 0,     // input deliverable, or nothing on screen to mislead
    Notice = 1,   // Connecting: honestly transitional, verdict pending
    Critical = 2, // terminal dead input under live video — unmissable red
};

inline constexpr qint64 kUndeliverablePressLatchMs = 6000;

// Running-edge debounce for the OVERLAY only. The direct pipe legitimately
// connects a beat after the session reaches Running (the first Running input
// tick seeds the pipe server), so an instantaneous overlay evaluation on the
// Running stateChanged would flash the red alarm on every healthy connect.
// The overlay therefore requires Running-with-pipe-down to either PERSIST for
// this grace or be PROVEN by an actual undeliverable press inside the window
// (a press is ground truth and bypasses the debounce on the same tick). The
// per-press "PRESS UNDELIVERABLE" log line stays instantaneous — a press in
// the grace window really is undeliverable and is still logged; only the
// steady-state screen alarm is debounced.
inline constexpr qint64 kRunningPipeDownOverlayGraceMs = 2500;

[[nodiscard]] inline constexpr bool runningPipeDownConfirmed(
    qint64 nowMs,
    qint64 pipeDownSinceMs,
    qint64 lastUndeliverablePressMs,
    qint64 graceMs = kRunningPipeDownOverlayGraceMs) noexcept
{
    return pipeDownSinceMs > 0
        && (nowMs - pipeDownSinceMs >= graceMs
            || lastUndeliverablePressMs >= pipeDownSinceMs);
}

[[nodiscard]] inline constexpr InputDeadSeverity inputDeadOverlaySeverity(
    RemotePlayState state,
    bool inputPipeEnabled,
    bool inputPipeConnected,
    bool videoAlive,
    bool sessionIntentActive,
    qint64 nowMs,
    qint64 lastUndeliverablePressMs) noexcept
{
    if (!pressUndeliverable(state, inputPipeEnabled, inputPipeConnected)) {
        return InputDeadSeverity::None;
    }
    if (!videoAlive) {
        return InputDeadSeverity::None;
    }
    const bool recentUndeliverablePress = lastUndeliverablePressMs > 0
        && nowMs >= lastUndeliverablePressMs
        && nowMs - lastUndeliverablePressMs <= kUndeliverablePressLatchMs;
    if (!sessionIntentActive && !recentUndeliverablePress) {
        return InputDeadSeverity::None;
    }
    if (state == RemotePlayState::Connecting) {
        return InputDeadSeverity::Notice;
    }
    return InputDeadSeverity::Critical;
}

} // namespace orion
