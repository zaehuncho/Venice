#pragma once

#include "OrionTypes.h"
#include "ShotIntentPolicy.h"

// Pure controller-routing policy shared by the Windows runtime and unit tests.
// Keeping these decisions independent of Win32/ViGEm makes the two safety
// boundaries deterministic and regression-testable:
//   * a just-unplugged ViGEm XInput slot must never be selected as a physical pad;
//   * once the patched Chiaki input pipe owns PS5 input, the desktop-visible
//     virtual pad is held neutral instead of mirroring gameplay buttons into Orion.

#include <QtCore/QtGlobal>

namespace orion {

inline constexpr qint64 kRetiredVirtualSlotQuarantineMs = 3000;
// Steam Input emits its mapped keyboard/mouse message on the same input beat as
// the physical press. A short edge-scoped guard catches that message without a
// held button (or noisy resting report) disabling the real mouse indefinitely.
inline constexpr qint64 kControllerUiGuardMs = 120;

[[nodiscard]] inline constexpr bool isVirtualXinputCandidate(
    int slot,
    int activeVirtualSlot,
    int retiredVirtualSlot,
    qint64 nowMs,
    qint64 retiredVirtualSlotUntilMs,
    bool noNavigationCapability) noexcept
{
    return noNavigationCapability
        || (activeVirtualSlot >= 0 && slot == activeVirtualSlot)
        || (retiredVirtualSlot >= 0 && slot == retiredVirtualSlot
            && nowMs <= retiredVirtualSlotUntilMs);
}

[[nodiscard]] inline constexpr bool shouldNeutralizeDesktopVirtualPad(
    bool sessionRunning,
    bool inputHookEnabled,
    bool inputHookConnected) noexcept
{
    return sessionRunning && inputHookEnabled && inputHookConnected;
}

// The Orion input pipe is created before Chiaki's console transport is ready.
// Connecting proves only that process/pipe startup is in progress; the first
// MUST_DELIVER ownership packet must wait until the current session has
// positively reached Running (Chiaki streaminfo observed). Otherwise the
// feedback sender is still uninitialized and a correctly fail-closed
// OrionStream terminates the session.
[[nodiscard]] inline constexpr bool directInputWriteAllowed(
    RemotePlayState state) noexcept
{
    return state == RemotePlayState::Running;
}

// Teardown/watchdog code may need to neutralize or relinquish a route after the
// session has already left Running. It may write only when this exact pipe
// generation previously completed an owned packet. A merely connected or
// never-seeded startup pipe must not be opened/seeded by cleanup code.
[[nodiscard]] inline constexpr bool directInputRouteCurrentlyOwned(
    bool pipeConnected,
    bool haveSent,
    bool lastPacketOwned) noexcept
{
    return pipeConnected && haveSent && lastPacketOwned;
}

// Raw Input commonly reports an empty device path after ViGEm has already
// removed its virtual HID. An unknown path must never be treated as "matches
// every physical controller"; only the exact cached handle or two known,
// equal paths prove that the active physical pad was removed.
[[nodiscard]] inline constexpr bool rawInputRemovalMatchesActivePhysical(
    bool handleMatches,
    bool removalPathKnown,
    bool activePathKnown,
    bool pathsEqual) noexcept
{
    return handleMatches
        || (removalPathKnown && activePathKnown && pathsEqual);
}

// [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Connect-time physical-pad gate, pure form.
//
// The 2026-08-07 tightening (admit only on a live route or a HID report in the
// last 3 s) was correct about the DEAD pad, but conflated two refusable states
// that need different handling:
//   * enumerated + silent  -> the pad is visible to Windows but its report
//     stream is idle. Live-log evidence (orion_native.log 2026-08-08 03:14 /
//     05:36 sessions) shows a healthy DualSense streams reports continuously
//     whenever it is enumerated, so a SILENT-but-enumerated pad is almost
//     always a USB-selective-suspend/resume artifact after system idle - a
//     state a wake probe (opening the HID collection) can clear. Refusing
//     outright forces the user into a retry loop.
//   * not enumerated + silent -> nothing to wake; Windows itself cannot see
//     the pad (observed live: pad absent from BOTH RawInput and WinMM until a
//     physical re-plug re-arrived it). Refuse with actionable guidance.
// The dead-pad hole stays closed because WakeProbeThenRecheck does NOT admit:
// after the probe, the caller re-evaluates this same gate and may proceed ONLY
// once a real report has landed (hasRecentRawInput()/physicalPadLive_). A pad
// that stays silent after the wake is refused exactly as before.
enum class PadConnectGateAction {
    Proceed,              // live route or fresh HID report - gate satisfied
    WakeProbeThenRecheck, // enumerated but silent - wake the device, then re-check for a REAL report
    RefuseNoController,   // not enumerated and silent - nothing to wake, refuse
};

[[nodiscard]] inline constexpr PadConnectGateAction padConnectGateAction(
    bool physicalPadLive,
    bool hasRecentRawReport,
    bool rawInputEnumerated) noexcept
{
    if (physicalPadLive || hasRecentRawReport) {
        return PadConnectGateAction::Proceed;
    }
    return rawInputEnumerated ? PadConnectGateAction::WakeProbeThenRecheck
                              : PadConnectGateAction::RefuseNoController;
}

// Shot automation may own/release an input only after the Remote Play session
// has positively reached Running AND the local virtual-pad route exists.  A
// capture-card preview deliberately satisfies neither condition: it is video
// and detection only.  Keeping this policy independent of remoteRunning_ is
// important because that UI/lifecycle flag is also true while Connecting.
[[nodiscard]] inline constexpr bool automationRouteReady(
    bool sessionRunning,
    bool virtualControllerConnected) noexcept
{
    return sessionRunning && virtualControllerConnected;
}

// [ORION_METER_DELAY_ROUTE_AUTHORITY 2026-08-08] What an engine disarm may take
// with it, split by CAUSE.
//
// syncEngineArmed() used to treat every disarm identically: revoke the
// controller-route attestation and zero the engine's echo. That is correct for
// every route/security cause (controller loss, transport recovery, delivery
// fault, security, shutdown, defense/safe mode) -- the attestation attests
// "this exact delivery route, this generation" and those causes genuinely
// invalidate it.
//
// It is WRONG for the meter-delay ramp gate. The inbound meter delay holds
// VIDEO packets; it cannot change the outbound controller delivery route, so
// the attestation stays true across a delay ramp. Revoking it forced a fresh
// neutral-local-delivery route proof, which only runs on a fully neutral
// controller tick -- during live play that window may not occur for minutes.
//
// MEASURED (orion_native.log 2026-08-08T22:32Z, owner rig): user changed the
// delay target 250->300 ms while Locked. publish() emitted settled=false for
// the 0.45 s re-ramp, syncEngineArmed() disarmed AND revoked, and every press
// for the next 72 seconds was benched as `waiting_for_latency_calibration`
// until 22:33:34Z when a neutral window finally re-issued proof generation=3.
// Disabling the delay entirely "fixed" it (the `!enabled()` short-circuit),
// which is exactly the "meter delay ON = bot dead" report.
//
// FAIL-CLOSED ARGUMENT: the disarm itself is preserved for BOTH causes -- an
// armed precise-fire token must never survive a delay ramp (the visible meter
// time-shifts under it) nor any route fault. Only the attestation teardown is
// scoped to the causes that actually falsify the attestation. A real route
// fault DURING a delay ramp still revokes: revokeRouteAttestation depends
// solely on routeAuthorityOk.
struct EngineArmDecision {
    bool arm = false;
    bool revokeRouteAttestation = false;
};

[[nodiscard]] inline constexpr EngineArmDecision engineArmDecision(
    bool routeAuthorityOk,   // every syncEngineArmed clause EXCEPT the meter-delay gate
    bool meterDelayGateOk) noexcept  // !meterDelay.enabled() || meterDelay.readyForArm()
{
    return EngineArmDecision{
        routeAuthorityOk && meterDelayGateOk,
        !routeAuthorityOk,
    };
}

// A route fault may be cleared only after the player has physically ended every
// supported shot gesture. Checking RS-Y alone is insufficient: a still-deflected
// stick can move sideways for three polls, clear the gate, then swing vertical and
// look like a fresh Go-To/Tempo edge without ever returning to center. Use the same
// raw threshold conversion as shot ownership, but require radial stick neutral so
// Button, TempoSquare, GoToStick, and TempoStick share one honest recovery edge.
[[nodiscard]] inline bool routeRecoveryShotInputsNeutral(
    const ControllerState& state,
    double stickUpThreshold,
    double stickDownThreshold) noexcept
{
    return shotInputControlsNeutral(
        state, stickUpThreshold, stickDownThreshold);
}

// A failed session promotion is a terminal input-authority boundary even when
// the capture-card preview sidecar deliberately stays alive. The preview owns
// pixels only; it must not retain the direct pipe or a ViGEm target.
[[nodiscard]] inline constexpr bool shouldReleaseInputRouteOnRemoteState(
    RemotePlayState state) noexcept
{
    return state == RemotePlayState::Error;
}

// RemotePlaySession snapshots whether an exit was unexpected while the
// process still belonged to a Running/Connecting session, then emits its
// terminal stateChanged(Disconnected, ...) before sidecarExited(). The
// controller's live-state mirror is therefore already false by the time the
// exit signal arrives and must not be used to decide whether recovery runs.
// Teardown/shutdown remain independent, synchronous intent boundaries.
[[nodiscard]] inline constexpr bool shouldRecoverUnexpectedSidecarExit(
    bool unexpectedWhileStreaming,
    bool remotePlayTeardownActive,
    bool applicationShutdownActive) noexcept
{
    return unexpectedWhileStreaming
        && !remotePlayTeardownActive
        && !applicationShutdownActive;
}

// A delayed full reconnect belongs to the exact lifecycle intent that
// scheduled it. A manual Connect/Disconnect (or a second recovery request)
// advances the generation synchronously, so an old timer must not tear down a
// newer Connecting/Running session when it eventually fires.
[[nodiscard]] inline constexpr bool delayedAutoReconnectStillCurrent(
    quint64 scheduledGeneration,
    quint64 currentGeneration,
    RemotePlayState state,
    bool autoReconnectEnabled,
    bool safeModeActive,
    bool applicationShutdownActive) noexcept
{
    return scheduledGeneration != 0
        && scheduledGeneration == currentGeneration
        && autoReconnectEnabled
        && !safeModeActive
        && !applicationShutdownActive
        && (state == RemotePlayState::Disconnected || state == RemotePlayState::Error);
}

enum class DesktopUiInputKind {
    Other,
    NavigationKey,
    MouseButton,
};

[[nodiscard]] inline constexpr bool controllerUiActivityPressEdge(
    quint32 buttons,
    int dpad,
    quint8 l2,
    quint8 r2,
    quint32 previousButtons,
    int previousDpad,
    quint8 previousL2,
    quint8 previousR2) noexcept
{
    constexpr quint8 triggerThreshold = 16;
    return (buttons & ~previousButtons) != 0
        || (dpad != 8 && dpad != previousDpad)
        || (l2 >= triggerThreshold && previousL2 < triggerThreshold)
        || (r2 >= triggerThreshold && previousR2 < triggerThreshold);
}

[[nodiscard]] inline constexpr bool shouldSuppressControllerMappedUiInput(
    bool streamActive,
    qint64 nowMs,
    qint64 guardUntilMs,
    DesktopUiInputKind kind) noexcept
{
    return streamActive
        && nowMs <= guardUntilMs
        && (kind == DesktopUiInputKind::NavigationKey
            || kind == DesktopUiInputKind::MouseButton);
}

} // namespace orion
