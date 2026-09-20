#pragma once

// Track W — watchdog cascade hardening.
//
// Pure, header-only policy + primitives shared by RemotePlaySession and its unit tests. Kept
// deliberately free of QtGui / Windows / RemotePlaySession so the tests can exercise the
// decision logic without linking RemotePlayCore.dll or the Qt GUI module (the test target links
// Qt6::Core only).
//
// Background (live forensics): a transient capture freeze (the PS5 briefly drops off the network
// so the Elgato reads a frozen picture) trips the native frame-stall watchdog, which fast-restarts
// the autogreen sidecar ~600ms later. In capture-card mode the Elgato HD60 X's DirectShow/MSMF
// handle from the just-killed sidecar has NOT released yet, so the respawn logs "no live device
// (index 0)" AND the native's own video-device enumeration (ICreateDevEnum, run on sidecar start
// to build ORION_VIDEO_DEVICE_NAMES) blocks synchronously on the contended device — on the GUI
// thread — for 10-15s, tripping "SAFE MODE: UI thread froze for over 6 seconds" and disarming
// automation. These two helpers break that cascade:
//   1. sidecarRestartDelayMs() — pace the capture-card restart so the handle releases first.
//   2. runBoundedEnumeration() — bound the enumeration so it can never wedge the GUI thread.

#include "AppConfig.h"

#include <QtCore/QStringList>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <limits>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>

namespace orion {

// --- Fix 1: watchdog restart pacing -------------------------------------------------------

// Decoder-pipe mode has no DirectShow device to release, so it keeps the historical fast beat
// that only needs the frame-export pipe to drain before the sidecar respawns.
inline constexpr int kSidecarRestartDelayMs = 600;

// Capture-card (Elgato HD60 X) mode: wait long enough for the DirectShow/MSMF handle from the
// torn-down sidecar to release before respawning. Respawning too soon makes the new sidecar hit
// "no live device (index 0)" and leaves the device contended, which is what then stalls the
// GUI-thread enumeration into SAFE MODE. Chosen from the 2500-4000ms release window observed in
// the live forensics.
inline constexpr int kSidecarRestartDelayMsCaptureCard = 3000;
// The normal disconnect path already waits for graceful sidecar shutdown.
// Keep a short, named post-teardown beat for any remaining DirectShow cleanup.
inline constexpr int kCapturePreviewResumeDelayMs = 1500;

// A watchdog restart can create a brand-new OrionStream top-level window long
// after the original Connect grace expired. Keep the native containment sweep
// alive across the full sidecar restart + Chiaki cold-start window in every video
// mode (capture-card still launches Chiaki for input).
inline constexpr qint64 kStreamWindowRestartContainmentGraceMs = 45'000;
// Lifecycle recovery must be keyed to RAW transport delivery, never detector
// uniqueness. A loading screen can be dark/static for an arbitrary duration
// while the capture backend continues delivering valid frames; pixel_age_ms is
// intentionally allowed to grow there and remains only an automation-freshness
// signal. The backend gets its in-process recovery attempt at ~2.5s; 8s plus
// one watchdog confirmation tick recovers a truly dead transport in ~9.5-10s
// without tearing down recoverable blips.
inline constexpr double kStreamTransportRestartAgeMs = 8'000.0;

[[nodiscard]] inline bool streamTransportNeedsRestart(
    double transportAgeMs, bool backendFrozen) noexcept
{
    return backendFrozen
        || (std::isfinite(transportAgeMs)
            && transportAgeMs >= 0.0
            && transportAgeMs > kStreamTransportRestartAgeMs);
}

// --- Direct-input recovery ---------------------------------------------------------------
//
// A healthy capture-card feed does not prove the independent Chiaki input child
// or its direct-injection pipe is alive. Give that smaller process three paced,
// capture-preserving recovery attempts first. If the pipe is still absent after
// one final grace interval, allow exactly one contained full-sidecar restart.
// Once that restart is armed, the caller holds automation fail-closed until a
// verified pipe reconnect or an explicit user session reset; the policy never
// requests another restart and therefore cannot recreate the historical Chiaki
// window respawn loop.
inline constexpr int kInputHookDownRecoverHeartbeats = 5;
inline constexpr int kInputHookRecoveryRetryHeartbeats = 15;
inline constexpr int kInputHookRecoveryMaxAttempts = 3;
inline constexpr int kInputHookFullRestartHeartbeat =
    kInputHookDownRecoverHeartbeats
    + kInputHookRecoveryMaxAttempts * kInputHookRecoveryRetryHeartbeats;

// An input-only recovery is deliberately shorter than a cold Remote Play launch: capture and the
// detector are already warm, and a current-session Chiaki handshake is the only missing authority.
// Bound the native Connecting state independently of Python so a blocked child-launch call cannot
// strand the UI or leave a late `ready` verdict eligible forever.
inline constexpr int kInputSessionRecoveryDeadlineMs = 20'000;

enum class InputLinkRecoveryAction {
    None,
    RecoverInputOnly,
    RestartSidecarContained,
    HoldFailClosed,
};

[[nodiscard]] inline constexpr InputLinkRecoveryAction inputLinkRecoveryAction(
    int downHeartbeats, int inputOnlyAttempts, bool fullRestartEscalated,
    bool inputOnlyRecoveryPending = false) noexcept
{
    if (fullRestartEscalated) {
        return InputLinkRecoveryAction::HoldFailClosed;
    }
    // The outer stream intentionally remains Running during an in-place child
    // repair. Suppress threshold re-evaluation until that bounded attempt has a
    // verdict, otherwise a slow (but healthy) recovery can consume another retry.
    if (inputOnlyRecoveryPending) {
        return InputLinkRecoveryAction::None;
    }

    const int boundedAttempts = std::clamp(
        inputOnlyAttempts, 0, kInputHookRecoveryMaxAttempts);
    if (boundedAttempts < kInputHookRecoveryMaxAttempts) {
        const int retryAt = kInputHookDownRecoverHeartbeats
            + boundedAttempts * kInputHookRecoveryRetryHeartbeats;
        return downHeartbeats >= retryAt
            ? InputLinkRecoveryAction::RecoverInputOnly
            : InputLinkRecoveryAction::None;
    }

    return downHeartbeats >= kInputHookFullRestartHeartbeat
        ? InputLinkRecoveryAction::RestartSidecarContained
        : InputLinkRecoveryAction::None;
}

// A recovery result is authoritative only for the one attempt that is still pending while the
// native stream generation remains Running. `rejectLate` is latched by timeout/error/disconnect
// and is cleared only when a genuinely new recovery generation begins.
[[nodiscard]] inline constexpr bool shouldAcceptInputRecoveryReady(
    bool pending, bool rejectLate, bool sessionRunning,
    bool hasExplicitInputReady, bool inputReady) noexcept
{
    return pending && !rejectLate && sessionRunning
        && hasExplicitInputReady && inputReady;
}

// Generation-scoped timer policy. A timer from an earlier recovery attempt must be inert even if a
// later attempt happens to be Running when it fires.
[[nodiscard]] inline constexpr bool inputRecoveryDeadlineApplies(
    quint64 timerGeneration, quint64 currentGeneration,
    bool pending, bool sessionRunning) noexcept
{
    return timerGeneration == currentGeneration && pending && sessionRunning;
}

// Running telemetry is authoritative revocation evidence for the independent
// Chiaki input child. Permit exactly one smallest-scope recovery attempt:
// recoverInputLink() retains Running, independently revokes route authority,
// and latches `pending`, so subsequent telemetry cannot request a duplicate
// child. Missing evidence is treated the same as an explicit false verdict and
// remains fail-closed.
[[nodiscard]] inline constexpr bool shouldAutoRecoverInputFromTelemetry(
    bool running, bool recoveryPending,
    bool hasExplicitInputReady, bool inputReady) noexcept
{
    return running && !recoveryPending
        && (!hasExplicitInputReady || !inputReady);
}

[[nodiscard]] inline constexpr qint64 extendStreamWindowContainmentGrace(
    qint64 currentDeadlineMs, qint64 nowMs) noexcept
{
    return std::max(currentDeadlineMs,
                    nowMs + kStreamWindowRestartContainmentGraceMs);
}
// stopSidecar returns as soon as the process exits; this is the maximum grace
// interval before a forced kill is required.
//
// GRACEFUL CHIAKI DISCONNECT: raised 2000 -> 5000. Every chiaki termination used to end in a
// TerminateProcess, so chiaki_session_stop() never ran and no Takion/ctrl disconnect was ever
// sent — the PS5 saw the transport simply vanish and showed "LAN cable disconnected". The python
// side now closes the session cleanly on the shutdown command (session stop + ctrl teardown +
// process wait), which needs more than 2s end to end; a 2s budget expired mid-teardown and the
// forced kill re-created the exact abrupt drop we are trying to remove. 5s covers the observed
// clean-exit path with margin while still bounding the disconnect click. The forced kill below
// stays as the last-resort safety net for a genuinely wedged sidecar.
inline constexpr int kSidecarGracefulShutdownMs = 5000;

// Capture-card mode is exactly the AppConfig source the sidecar-env build reads (videoSource ==
// "capture_card"). Centralised here so the restart pacing and the ORION_CAPTURE_CARD env gate can
// never disagree about which mode we are in. Case-insensitive to match the env gate's compare.
[[nodiscard]] inline bool isCaptureCardSource(const AppConfigData& config)
{
    return !isXboxRemotePlay(config)
        && config.videoSource.compare(QStringLiteral("capture_card"), Qt::CaseInsensitive) == 0;
}

// The delay the watchdog waits after tearing the sidecar down before respawning it.
[[nodiscard]] inline constexpr int sidecarRestartDelayMs(bool captureCardMode) noexcept
{
    return captureCardMode ? kSidecarRestartDelayMsCaptureCard : kSidecarRestartDelayMs;
}

// --- Connect: reuse the warm preview sidecar instead of tearing it down + cold-rebuilding -------
//
// The slow Chiaki Connect was: a WARM live-preview sidecar (capture card + detector already
// running) is KILLED (stopSidecar: terminate + waitForFinished + a 2-pass taskkill sweep) and a
// BRAND-NEW sidecar is cold-started after the ~3s capture-card release beat (kSidecarRestartDelayMs
// CaptureCard) — re-opening the single-open Elgato from scratch — before Chiaki even begins its
// ~9s cold start. That whole teardown+respawn (~5-6s) is pure waste when the warm sidecar's video
// SOURCE is unchanged: in capture-card mode Chiaki is INPUT-ONLY (the card is the video), so the
// warm sidecar can simply be COMMANDED to launch Chiaki/input in place (a stdin start_stream cmd,
// mirroring the update_remap hot-apply) while its card feed + detector keep flowing uninterrupted.
//
// Reuse ONLY when the warm sidecar's source matches the requested connect source — both capture
// card. A live-preview sidecar only ever exists in capture-card mode (startCapturePreview no-ops
// otherwise), so warmSourceIsCaptureCard is the "a warm preview is up" signal; if the user switched
// the source away from the capture card between preview and Connect (connectSourceIsCaptureCard ==
// false) the feed genuinely differs and a full rebuild is still correct.
[[nodiscard]] inline bool shouldReuseWarmSidecarForConnect(bool warmPreviewRunning,
                                                           bool warmSourceIsCaptureCard,
                                                           bool connectSourceIsCaptureCard) noexcept
{
    return warmPreviewRunning && warmSourceIsCaptureCard && connectSourceIsCaptureCard;
}

// A preview-sidecar `started` event means only that capture + detection are
// available, so it remains usable without an input session. Any event that can
// transition a real stream to Running must carry explicit current-session
// readiness; accepting a missing legacy field recreates the process/pipe-alive
// false authority that armed automation while the PS5 handshake was timing out.
[[nodiscard]] inline constexpr bool sidecarStartedHasInputAuthority(
    bool previewMode, bool hasExplicitInputReady, bool inputReady) noexcept
{
    return previewMode || (hasExplicitInputReady && inputReady);
}

// A failed warm capture-card promotion must drop only the unproven Chiaki/input child. The already
// live HDMI capture + detector sidecar remains a valid preview and should be handed back to the UI;
// no-card/decoder mode and a dead sidecar have nothing safe to preserve.
[[nodiscard]] inline constexpr bool shouldRestoreWarmPreviewAfterPromotionFailure(
    bool promotedWarmPreview, bool sidecarRunning, bool captureCardSource) noexcept
{
    return promotedWarmPreview && sidecarRunning && captureCardSource;
}

// --- RC-2b: sidecar detection-emit gate (frame_count dedupe vs the stall override) ---------
//
// RemotePlaySession dedupes sidecar detection emissions on frame_count so a re-published
// consensus for the SAME frame can't double-feed the engine's velocity/timing state. But the
// sidecar's pixel-age STALL override (autogreen_sidecar.py) synthesizes a meter_present:false
// emission while frame_count is FROZEN — no new unique frame is exactly what a stall is — so
// the dedupe silently discarded every stall emission and the meter-lost reset chain (the
// 293s-HOLD-bug fix) didn't fire until the next unique frame arrived. A no-meter emission
// carries no fill/velocity that could double-feed timing (the engine only resets off it), so
// it is safe — and required — to bypass the frame_count dedupe when meter_present is false.
// hasDetectionSignal = the classic (fill > 0 || confidence > 0) content gate.
[[nodiscard]] inline constexpr bool shouldAcceptSidecarDetectionEmission(
    bool hasDetectionSignal, bool meterPresent,
    int frameCount, int lastAcceptedFrameCount) noexcept
{
    if (!meterPresent) {
        return true;   // stall / meter-lost override: accept even on a frozen frame_count
    }
    return hasDetectionSignal && frameCount != lastAcceptedFrameCount;
}

// Capture age reported by the sidecar is measured before JSON/stdout transit. A
// backed-up preview pipe can therefore deliver an apparently 6ms-old detection
// hundreds of milliseconds later. When an epoch capture timestamp is available,
// include the full capture->native-receipt delay. Invalid/future/implausibly old
// epochs fail closed (infinity), while older sidecars without capture_ts retain
// the validated reported age.
[[nodiscard]] inline double effectiveSidecarFrameAgeMs(double reportedAgeMs,
                                                       double captureEpochMs,
                                                       double receiptEpochMs) noexcept
{
    if (!std::isfinite(reportedAgeMs) || reportedAgeMs < 0.0) {
        return std::numeric_limits<double>::infinity();
    }
    if (!(captureEpochMs > 0.0)) {
        return reportedAgeMs;
    }
    if (!std::isfinite(captureEpochMs) || !std::isfinite(receiptEpochMs)
        || receiptEpochMs <= 0.0) {
        return std::numeric_limits<double>::infinity();
    }
    const double transitAgeMs = receiptEpochMs - captureEpochMs;
    constexpr double kMaxPlausibleEpochAgeMs = 24.0 * 60.0 * 60.0 * 1000.0;
    if (!std::isfinite(transitAgeMs) || transitAgeMs < 0.0
        || transitAgeMs > kMaxPlausibleEpochAgeMs) {
        return std::numeric_limits<double>::infinity();
    }
    return std::max(reportedAgeMs, transitAgeMs);
}

// --- Fix 2: non-blocking video-device enumeration -----------------------------------------

// Hard cap on how long a sidecar (re)start may spend enumerating video-input device names on the
// calling (GUI) thread. The DirectShow enumeration can wedge for 10-15s when the Elgato is
// contended right after a watchdog restart; past this deadline we abandon the wait and fall back
// to the last-good cached names so ORION_VIDEO_DEVICE_NAMES is still populated and the UI thread
// is never frozen.
inline constexpr int kVideoEnumTimeoutMs = 1500;

// Run `enumerate` on a detached worker and block the caller for at most timeoutMs. Returns the
// fresh result if the worker finished in time, otherwise `fallback` (the worker keeps running and
// frees itself via the shared state; it never touches caller state, so a late finish is race-free
// and simply discarded). Templated + header-only so tests can inject a fake slow/fast enumerator
// and assert the caller is never blocked past the deadline.
template <typename Result, typename EnumFn>
[[nodiscard]] Result runBoundedEnumeration(int timeoutMs,
                                           const Result& fallback,
                                           EnumFn enumerate)
{
    struct Shared {
        std::mutex m;
        std::condition_variable cv;
        bool done = false;
        Result names;
    };
    auto shared = std::make_shared<Shared>();

    std::thread([shared, fn = std::move(enumerate)]() mutable {
        Result result = fn();
        {
            std::lock_guard<std::mutex> lk(shared->m);
            shared->names = std::move(result);
            shared->done = true;
        }
        shared->cv.notify_all();
    }).detach();

    std::unique_lock<std::mutex> lk(shared->m);
    if (shared->cv.wait_for(lk, std::chrono::milliseconds(timeoutMs),
                            [&shared]() { return shared->done; })) {
        return shared->names;  // finished in time — use the fresh names
    }
    return fallback;  // timed out — reuse the last-good cache; the worker finishes & self-frees
}

} // namespace orion
