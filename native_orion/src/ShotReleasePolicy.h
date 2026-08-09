#pragma once

#include "OrionTypes.h"

namespace orion {

// Canonical first packet for a committed shot release. This changes only the
// control Orion owns for the active mode; movement, triggers, D-pad, and every
// unrelated button remain byte-for-byte as supplied by the caller.
//
// Tempo releases are direction-change edges, not neutral edges. Both the
// Square-triggered remap and raw TempoStick own RS-down (+127) and must submit
// exactly one RS-up (-127) flick at the deadline.
//
// FADE EXCEPTION (2026-08-06). The rule above used to be unconditional — the
// shotType argument was explicitly discarded here with the comment "every shot
// type uses the same Tempo gesture". That produced a reproducible STEP-BACK
// instead of a fade whenever Tempo was enabled, and only with Tempo (the owner
// confirmed the bug disappears with the remap off).
//
// Reason: in this game the gather direction SELECTS THE MOVE. Pro Stick down
// while the player holds a movement direction is the escape/step-back input; the
// fade is the mirrored gesture. Holding RS-down for a fade therefore asks the
// game for a step-back and the game correctly obliges.
//
// This is not deduced — it is how every working implementation does it. Across 16
// independent Cronus Zen scripts in the owner's collection, all 16 zero RS-X
// during shot output, and the ones with a fade branch use the exact mirror:
//   normal:  gather RY +100 (down), release flick up
//   fade:    gather RY -100 (up),   release flick DOWN   (rs_down_combo)
// Verified in Cronet_Glory_V4.2 (fade branch at ~line 1614), and replicated
// independently in yewscripts-Hoops and Cronet_Hoops_V1.
//
// So classification still may not reverse the gesture MID-PRESS — the gather and
// the release are chosen from the same shotType and stay consistent for the whole
// shot. What changed is that a fade now runs the mirrored gesture end to end.
//
// tempoFadeMirrorGesture exists to make this one-flag revertible if a live batch
// says otherwise; it is ON by default so it is actually exercised rather than
// becoming another never-enabled flag.

[[nodiscard]] inline bool tempoGestureIsFade(const QString& shotType,
                                             bool mirrorEnabled) noexcept
{
    return mirrorEnabled
        && shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive);
}

// isFadeGesture is the ALREADY-DECIDED gesture for this press, not the config flag.
// Callers that own a shot pass the arm-time latch (ShotContext::tempoFadeGesture);
// pre-arm/pass-through callers pass tempoGestureIsFade(type, flag) explicitly. Keeping
// the decision out of this function is deliberate: it is what guarantees the release
// flick cannot disagree with the gather that preceded it.
inline void applyShotReleaseEdge(ControllerState& output,
                                 ShotMode mode,
                                 const QString& shotType,
                                 bool isFadeGesture = false) noexcept
{
    static_cast<void>(shotType); // retained for call-site readability/telemetry only
    // A fade releases by flicking DOWN (+127), the mirror of the normal RS-up
    // flick, because it gathered UP. See the fade-exception note above.
    const int tempoFlick = isFadeGesture ? 127 : -127;
    switch (mode) {
    case ShotMode::ButtonShot:
        // Square is the owned shot control; leave both sticks and every other
        // button untouched.
        output.buttons &= ~XINPUT_GAMEPAD_X;
        break;
    case ShotMode::GoToStick:
        // [ORION_GOTO_DOWN_FLICK 2026-08-09] Owner directive: "go to can absolutely use tempo
        // but the logic is reversed. you flick down towards the meter as of the regular tempo
        // path you flick up."
        //
        // Go-To GATHERS RS-up (-127 — see AutomationEngine's applyHeld and forceHeldOutput,
        // both of which hold -127 for this mode), so by the mirror rule documented above it
        // must RELEASE by flicking DOWN. This was the only gather-up gesture in the engine
        // that released at neutral instead of mirroring, and neutral is a return-to-rest, not
        // the direction-change edge the game reads as a shot release. Go-To has never once
        // released successfully on the owner's rig.
        output.rightStickX = 0;
        output.rightStickY = 127;
        break;
    case ShotMode::TempoStick:
        // The held gather releases on its first opposing flick packet.
        output.rightStickX = 0;
        output.rightStickY = tempoFlick;
        break;
    case ShotMode::TempoSquare:
        // Square only selects the remap and remains suppressed. The actual shot
        // edge is the same deliberate flick used by raw TempoStick.
        output.buttons &= ~XINPUT_GAMEPAD_X;
        output.rightStickX = 0;
        output.rightStickY = tempoFlick;
        break;
    }
}

// Final semantic invariant for the immutable precise-fire mailbox payload.
//
// This is intentionally narrower than comparing the packet with a freshly
// generated release edge: movement, triggers, D-pad, and unrelated buttons are
// user-owned and may legitimately change before the deadline.  Only the
// control that defines the selected shot mode is authoritative here.  The
// precise-fire worker checks this again under its final submit fence so a stale
// held packet or mode/output mismatch can never be accepted by either route.
[[nodiscard]] inline bool isValidShotReleaseOutput(
    const ControllerState& output, ShotMode mode) noexcept
{
    switch (mode) {
    case ShotMode::ButtonShot:
        return !output.square();
    case ShotMode::GoToStick:
        // [ORION_GOTO_DOWN_FLICK 2026-08-09] Must move in lockstep with the down flick
        // applyShotReleaseEdge now emits. Leaving this pinned at neutral would have rejected
        // every Go-To release at the final submit fence and silently dropped the shot — the
        // exact no-fire failure the fade note below records happening once already.
        //
        // This also closes a hole. The old predicate accepted a fully NEUTRAL packet, so a
        // blank or stale payload satisfied Go-To's fence while every other mode demanded a
        // full-scale flick with X centred. Go-To now carries the same burden of proof.
        return output.rightStickX == 0 && output.rightStickY == 127;
    // [ORION_TEMPO_FADE_MIRROR] Both full-scale flick directions are now legitimate
    // release edges: a normal shot gathers DOWN and releases UP (-127); a fade gathers
    // UP and releases DOWN (+127). Pinning -127 here would have rejected every fade
    // release at the final submit fence and silently dropped the shot — turning the
    // step-back fix into a no-fire bug.
    //
    // What this still proves (the failure shapes this fence exists for):
    //   * a stale held BUTTON packet — square set / sticks at rest — still fails
    //   * a packet generated for a DIFFERENT stick mode still fails
    //   * a neutral or partial-deflection packet still fails; only a full-scale
    //     +-127 flick with X exactly centred passes
    // What it no longer distinguishes: a GATHER packet from the opposite shot type's
    // RELEASE packet, since they share magnitude and now only differed by sign. That
    // is acceptable because the mailbox is armed from applyShotReleaseEdge's output by
    // construction, so a gather could only appear here via an independent arming bug,
    // and the arm path is itself fenced by token/route/generation checks.
    case ShotMode::TempoStick:
        return output.rightStickX == 0
            && (output.rightStickY == -127 || output.rightStickY == 127);
    case ShotMode::TempoSquare:
        return !output.square()
            && output.rightStickX == 0
            && (output.rightStickY == -127 || output.rightStickY == 127);
    }
    return false;
}

} // namespace orion
