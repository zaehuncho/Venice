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

// === [ORION_TEMPO_RELEASE_STYLE 2026-09-15 owner] RHYTHM'S RELEASE EDGE ====================
//
// Flick (default, and every build before this existed): the owned gather is ended with one
// full-scale OPPOSING deflection — the direction-change edge documented above.
//
// LetGo: at the SAME release-decision instant the stick is driven to NEUTRAL and held there for
// the same pulse window instead of flicking the other way. The player keeps the stick pulled
// down and Venice performs the let-go for them, so the console sees a clean stick-RELEASE edge.
//
// NOTHING ELSE MOVES. The arm, the hold law, the vision timing, the hold band, the blind
// backstop, the trims, the learners and the latency marker all work on the release INSTANT, and
// the instant is identical under both styles — only the packet differs. The still-held physical
// stick is covered by the existing ReleasedUntilPhysicalEnd drain and the three-neutral-poll
// rearm fence: applyShotReleaseEdge keeps writing 0 over the user's deflection for the whole
// drain, so the let-go cannot be immediately undone by the hand that is still holding down.
enum class TempoReleaseStyle { Flick, LetGo };

// Ignore-unknown, never guess: anything that is not exactly "letgo" is the shipped flick. The
// failure mode of guessing here is a release packet the console does not read as a shot at all.
[[nodiscard]] inline TempoReleaseStyle tempoReleaseStyleFromString(const QString& style) noexcept
{
    return style.compare(QStringLiteral("letgo"), Qt::CaseInsensitive) == 0
        ? TempoReleaseStyle::LetGo
        : TempoReleaseStyle::Flick;
}

[[nodiscard]] inline const char* tempoReleaseStyleToken(TempoReleaseStyle style) noexcept
{
    return style == TempoReleaseStyle::LetGo ? "letgo" : "flick";
}

// isFadeGesture is the ALREADY-DECIDED gesture for this press, not the config flag.
// Callers that own a shot pass the arm-time latch (ShotContext::tempoFadeGesture);
// pre-arm/pass-through callers pass tempoGestureIsFade(type, flag) explicitly. Keeping
// the decision out of this function is deliberate: it is what guarantees the release
// flick cannot disagree with the gather that preceded it.
inline void applyShotReleaseEdge(ControllerState& output,
                                 ShotMode mode,
                                 const QString& shotType,
                                 bool isFadeGesture = false,
                                 TempoReleaseStyle tempoStyle = TempoReleaseStyle::Flick) noexcept
{
    static_cast<void>(shotType); // retained for call-site readability/telemetry only
    // A fade releases by flicking DOWN (+127), the mirror of the normal RS-up
    // flick, because it gathered UP. See the fade-exception note above.
    // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] ...unless the owner chose "letgo", in which case the
    // release edge is the stick RETURNING TO REST (0) rather than deflecting the other way. The
    // default argument is Flick, so every caller that does not opt in is byte-identical.
    const int tempoFlick = tempoStyle == TempoReleaseStyle::LetGo
        ? 0
        : (isFadeGesture ? 127 : -127);
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
// [ORION_TEMPO_RELEASE_STYLE 2026-09-15] The fence must move in lockstep with the edge
// applyShotReleaseEdge emits, exactly as it had to for the Go-To down flick and the fade mirror:
// under "letgo" the legitimate tempo release packet IS the neutral one, and pinning ±127 here
// would reject every let-go release at the final submit fence and silently drop the shot. Under
// "flick" the predicate is unchanged, so a stale held BUTTON packet, a packet built for another
// stick mode and a partial deflection all still fail exactly as before. What the let-go branch
// gives up is the ability to distinguish a NEUTRAL release packet from a blank one — acceptable
// for the same reason the fade mirror gave up gather-vs-release: the mailbox is armed from
// applyShotReleaseEdge's own output by construction, and the arm path is fenced by
// token/route/generation checks.
[[nodiscard]] inline bool isValidShotReleaseOutput(
    const ControllerState& output, ShotMode mode,
    TempoReleaseStyle tempoStyle = TempoReleaseStyle::Flick) noexcept
{
    const bool letGo = tempoStyle == TempoReleaseStyle::LetGo;
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
            && (letGo ? output.rightStickY == 0
                      : (output.rightStickY == -127 || output.rightStickY == 127));
    case ShotMode::TempoSquare:
        return !output.square()
            && output.rightStickX == 0
            && (letGo ? output.rightStickY == 0
                      : (output.rightStickY == -127 || output.rightStickY == 127));
    }
    return false;
}

} // namespace orion
