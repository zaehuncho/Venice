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
    RemotePlayState state, bool inputRecoveryPending = false) noexcept
{
    // Input-only recovery retains Running for video/history continuity. A newly
    // created pipe is not an initialized console transport; even a neutral seed
    // must wait for the current recovery attempt's positive readiness verdict.
    return state == RemotePlayState::Running && !inputRecoveryPending;
}

// [ORION_INPUT_DEAD_UX 2026-08-30] SINGLE SOURCE OF TRUTH for "a physical press
// cannot reach the console right now". In capture-card mode the HDMI video keeps
// flowing whatever the Chiaki INPUT session does, so a Disconnected/Error/
// Connecting session looks completely alive on screen while every button is
// dead (measured 08-28..08-30: 31 of 185 Square-edge epochs landed in exactly
// such windows, worst 2m18s of active play against dead input). Both the
// per-press "PRESS UNDELIVERABLE" forensic line and the on-screen input-dead
// overlay MUST call this one predicate so the log and the screen can never
// disagree. It deliberately reuses directInputWriteAllowed() — the fail-closed
// write gate stays the authority; this only NAMES its refusal.
//
//   * !Running session  -> no route exists (writes are refused before Running);
//   * Running but the direct pipe route is enabled and not connected -> the
//     authoritative route is down (heartbeat recovery may be in flight).
[[nodiscard]] inline constexpr bool pressUndeliverable(
    RemotePlayState state,
    bool inputPipeEnabled,
    bool inputPipeConnected) noexcept
{
    return !directInputWriteAllowed(state)
        || (inputPipeEnabled && !inputPipeConnected);
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

// ── [ORION_DISCONNECT_AUDIT 2026-09-19] ─────────────────────────────────────
// The three lifecycle decisions the 09-19 teardown audit added, as pure form so
// each can be argued and regression-tested without a QProcess or a Qt GUI.

// F3. A sidecar QProcess::finished belongs to a RETIRED generation when a DIFFERENT
// process is already installed as the live one. The normal teardown is deliberately
// NOT this case: stopSidecar() nulls the pointer before it waits, so an in-call
// finished sees "no live process" and must still run the full crash classification
// and sidecarExited() emission. Only a process that outlived waitForFinished +
// taskkill /F + kill() -- and whose exit therefore lands after the 600/3000 ms
// respawn beat installed a NEW sidecar -- may be ignored, because letting it run
// nulls the LIVE process pointer, latches rejectLateSidecarStarted_ against the new
// sidecar's `started`, and emits a spurious exit at the new session.
[[nodiscard]] inline constexpr bool sidecarExitBelongsToRetiredGeneration(
    bool haveLiveSidecarProcess,
    bool liveSidecarProcessIsThisOne) noexcept
{
    return haveLiveSidecarProcess && !liveSidecarProcessIsThisOne;
}

// F7. The deferred preview->stream handoff timer. `state == Connecting` is a LEVEL,
// not an identity: it cannot tell "still the connect that armed me" from "a later
// connect that also happens to be Connecting". The intent generation is bumped by
// start() and stop() ONLY -- deliberately not by the promote/recovery generations,
// which process-level events also bump, so a late exit of the very process this
// handoff is waiting to replace cannot cancel the handoff and strand Connecting.
[[nodiscard]] inline constexpr bool deferredSidecarHandoffStillCurrent(
    quint64 armedIntentGeneration,
    quint64 currentIntentGeneration,
    RemotePlayState state) noexcept
{
    return armedIntentGeneration == currentIntentGeneration
        && state == RemotePlayState::Connecting;
}

// F8. A sidecar restart may never race a teardown. Every call site is independently
// gated on a live session today, so this is defence in depth -- but the restart path
// re-arms the embed watchdog that disconnectRemotePlay() just stopped and respawns
// the sidecar, which is exactly the shape of "I pressed Disconnect and it came back".
[[nodiscard]] inline constexpr bool sidecarRestartAllowedDuringLifecycle(
    bool applicationShutdownActive,
    bool remotePlayTeardownActive) noexcept
{
    return !applicationShutdownActive && !remotePlayTeardownActive;
}

enum class DesktopUiInputKind {
    Other,
    NavigationKey,
    MouseButton,
    // [ORION_CONTROLLER_UI_ISOLATION 2026-09-19] The two kinds the 2026-09-11
    // press-edge guard never covered. Owner report: "when using the controller
    // you can see it moving on the pc hovering over stuff" -- the visible symptom
    // is the POINTER walking across Venice's controls and lighting up hover
    // states, which is WM_MOUSEMOVE, not a mapped key or a click. Steam Input's
    // desktop configuration (and DS4Windows/DualSenseX) map an analog STICK to
    // the mouse cursor and the other stick to the wheel; neither produces the
    // button/D-pad/trigger edge that opens the old 120 ms guard, so every such
    // message sailed straight into the QML scene.
    PointerMotion,
    WheelScroll,
};

// Where a desktop UI message came from, as reported by Win32
// GetCurrentInputMessageSource() while the message is being processed.
//   * Hardware  -> IMO_HARDWARE: a real mouse/keyboard attached to this PC.
//   * Injected  -> IMO_INJECTED/IMO_SYSTEM: SendInput() from another process.
//                  Steam Input, DS4Windows and DualSenseX all translate pad
//                  reports this way, so this is the exact discriminator between
//                  "the owner touched his mouse" and "the pad drove the cursor".
//   * Unknown   -> the API was unavailable or failed; fall back to the timing
//                  guard rather than guessing.
enum class DesktopUiInputOrigin {
    Unknown,
    Hardware,
    Injected,
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

// ── [ORION_CONTROLLER_UI_ISOLATION 2026-09-19] ───────────────────────────────
// A stick pushed past the reader's own noise floor is the activity that drives a
// mapped POINTER, and it never produces a press edge. normalizeHidAxis() already
// zeroes |axis| < 8 before this sees it, so this threshold only has to clear
// resting jitter on a worn stick; ~19 % deflection is deliberate movement.
inline constexpr int kControllerUiStickDeflection = 24;
// Longer than the 120 ms press guard because a mapped pointer keeps emitting
// WM_MOUSEMOVE with its own smoothing tail after the stick recenters.
inline constexpr qint64 kControllerUiPointerGuardMs = 220;

[[nodiscard]] inline constexpr bool controllerUiPointerActivity(
    int leftStickX,
    int leftStickY,
    int rightStickX,
    int rightStickY,
    int deflection = kControllerUiStickDeflection) noexcept
{
    const auto past = [deflection](int axis) noexcept {
        return axis >= deflection || axis <= -deflection;
    };
    return past(leftStickX) || past(leftStickY)
        || past(rightStickX) || past(rightStickY);
}

[[nodiscard]] inline constexpr bool desktopUiInputKindDrivesUi(
    DesktopUiInputKind kind) noexcept
{
    return kind != DesktopUiInputKind::Other;
}

// The ONE decision behind the native event filter. Split by cause so each leg
// can be argued on its own:
//
//   * streamActive == false           -> never suppress. Outside a live PS5
//     session the pad is not "gameplay input" and the owner may legitimately be
//     driving Venice with whatever Steam maps.
//   * controllerUiModeActive == true  -> never suppress. The explicit escape
//     hatch for any deliberately controller-driven UI (setup tour / a future
//     "configure controller" mode) and for an owner who wants the old
//     behaviour back (ORION_CONTROLLER_UI_PASSTHROUGH=1).
//   * origin == Injected              -> suppress every UI-driving kind, with no
//     timing window at all. During a live session an injected mouse/keyboard
//     message is by construction not the owner's real mouse; this is the leg
//     that finally covers pointer motion and the wheel, and it closes the
//     ordering race in the old design (the mapped message could be dispatched
//     before the 4 ms input poll observed the edge that opens the guard).
//   * origin == Hardware -> pass while source isolation is enabled. UIAccess
//     injection can also report hardware; test the actual mapper.
//   * unknown origin (or source isolation disabled) -> the legacy key/click
//     timing guard plus the stick-deflection pointer/wheel guard.
[[nodiscard]] inline constexpr bool shouldIsolateDesktopUiInput(
    bool streamActive,
    bool controllerUiModeActive,
    bool injectedIsolationEnabled,
    DesktopUiInputOrigin origin,
    qint64 nowMs,
    qint64 pressGuardUntilMs,
    qint64 pointerGuardUntilMs,
    DesktopUiInputKind kind) noexcept
{
    if (!streamActive || controllerUiModeActive || !desktopUiInputKindDrivesUi(kind)) {
        return false;
    }
    if (injectedIsolationEnabled) {
        if (origin == DesktopUiInputOrigin::Injected) {
            return true;
        }
        // A positive hardware-origin result is not a timing heuristic. Let the
        // real mouse/keyboard work even while a stick keeps the guard active.
        // Unknown origins retain the timing fallback; disabling source isolation
        // explicitly restores the legacy heuristic for hardware-reporting mappers.
        if (origin == DesktopUiInputOrigin::Hardware) {
            return false;
        }
    }
    switch (kind) {
    case DesktopUiInputKind::NavigationKey:
    case DesktopUiInputKind::MouseButton:
        // Reuse the shipped predicate verbatim so the two can never diverge.
        return shouldSuppressControllerMappedUiInput(
            streamActive, nowMs, pressGuardUntilMs, kind);
    case DesktopUiInputKind::PointerMotion:
    case DesktopUiInputKind::WheelScroll:
        return nowMs <= pointerGuardUntilMs;
    case DesktopUiInputKind::Other:
        break;
    }
    return false;
}

// [ORION_PAD_SILENT_HOLD 2026-09-11] Grace before the virtual pad is torn down once the
// physical pad's report stream stops, decided by CASE:
//   * enumerated + silent -> the pad is still on the bus. Live evidence (orion_native.log
//     2026-09-11 20:52:13, 20:52:21, 21:07:18, 21:07:23 and eleven more in the rotated log):
//     a USB DualSense on a port whose EnhancedPowerManagementEnabled default was still 1
//     went silent for 4-52 s MID-PLAY with no PnP removal, then resumed on its own. The old
//     2.5 s teardown was the only thing the console ever saw - the "random controller
//     disconnect". The virtual pad is already submitted NEUTRAL at silence onset (the
//     stuck-input safety), so holding it connected costs nothing: hold for 30 s and let the
//     poll nudge the HID collection once (open + input-report poll, the same wake the
//     connect gate uses).
//   * not enumerated -> a real unplug (GIDC_REMOVAL). Nothing to wake; keep the 2.5 s
//     armed / 8 s idle teardown so a Square held at unplug never stays latched.
inline constexpr qint64 kSilentEnumeratedPadHoldMs = 30000;
[[nodiscard]] inline constexpr qint64 silentPadTeardownGraceMs(
    bool enumerated,
    bool armedOrOwning) noexcept
{
    if (enumerated) {
        return kSilentEnumeratedPadHoldMs;
    }
    return armedOrOwning ? 2500 : 8000;
}

} // namespace orion
