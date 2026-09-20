#pragma once

#include "OrionTypes.h"

#include <algorithm>
#include <cmath>

namespace orion {

// ControllerState stick axes are normalized to [-127, 127], while the
// configured thresholds are percentages. Keep this conversion shared with
// AutomationEngine so the early reader wake and the later hold-to-own decision
// cross on the exact same physical sample.
inline constexpr double kStickUnitScale = 1.27;

[[nodiscard]] inline double rawStickThreshold(double configuredThreshold) noexcept
{
    return std::abs(configuredThreshold) * kStickUnitScale;
}

// Hardware intent is deliberately detected before AutomationEngine's hold-to-own
// debounce.  The debounce protects controller ownership from quick dribble/pump
// gestures, but the vision reader must be awake on the first physical input frame or
// it can miss the meter's appearance/early trajectory.  Only vertical-dominant stick
// gestures qualify; horizontal/diagonal dribbles never open the reader gate.
struct ShotIntentEdges {
    bool square = false;
    bool stickUp = false;
    bool stickDown = false;

    [[nodiscard]] bool any() const noexcept
    {
        return square || stickUp || stickDown;
    }
};

// [ORION_RHYTHM_STICK_PULL 2026-09-15] THE ONE spelling of the intent label, shared by the
// "Physical shot epoch: ... intent=" forensic line and by the `shot_gate_arm send: source=`
// field the sidecar receives as `src=`. It used to be an inline ternary in the middle of
// OrionAppController's controller tick, which is exactly where a fourth gesture (or a rename)
// would have produced two spellings of the same edge and made a session log unjoinable.
//
// The values are WIRE VALUES: the sidecar logs them and post-mortem tooling greps them, so they
// are append-only. `square` wins a simultaneous read because a Square edge is a committed shot
// press while a stick deflection is still a gesture.
[[nodiscard]] inline const char* shotIntentSourceLabel(const ShotIntentEdges& edges) noexcept
{
    if (edges.square) {
        return "square_edge";
    }
    return edges.stickUp ? "stick_up_edge" : "stick_down_edge";
}

// Canonical right-stick shot predicate shared by the first-edge reader wake
// and AutomationEngine's later hold-to-own debounce.  Keeping the vertical
// threshold and lateral-dominance rule here prevents a diagonal dribble from
// being owned by automation after the reader correctly refused to wake (or the
// inverse: a real vertical shot waking the reader but never becoming owned).
[[nodiscard]] inline bool verticalStickShotIntent(const ControllerState& state,
                                                  bool up,
                                                  double threshold,
                                                  double lateralMaxRatio) noexcept
{
    const double y = static_cast<double>(state.rightStickY);
    const double x = static_cast<double>(state.rightStickX);
    const double rawThreshold = rawStickThreshold(threshold);
    const bool crosses = up ? y <= -rawThreshold : y >= rawThreshold;
    const double lateralLimit = std::max(1.0, lateralMaxRatio * std::abs(y));
    return crosses && std::abs(x) <= lateralLimit;
}

// Canonical physical-end predicate for every shot-control recovery boundary.
// A selected controller report is required: transport absence is not evidence
// that Square was released or that the right stick returned to center.
[[nodiscard]] inline bool shotInputControlsNeutral(
    const ControllerState& state,
    double stickUpThreshold,
    double stickDownThreshold) noexcept
{
    const double neutralRadius = std::min(
        rawStickThreshold(stickUpThreshold),
        rawStickThreshold(stickDownThreshold));
    return !state.square()
        && std::hypot(static_cast<double>(state.rightStickX),
                      static_cast<double>(state.rightStickY)) < neutralRadius;
}

inline ShotIntentEdges shotIntentEdges(const ControllerState& current,
                                       const ControllerState& previous,
                                       double stickUpThreshold,
                                       double stickDownThreshold,
                                       double lateralMaxRatio = 0.70) noexcept
{
    // Cross is the in-game call-for-ball/pass request.  It is competing context
    // for a Go-To and must never mint a reader-wake epoch from a simultaneous
    // RS-up sample.  The stateful tracker below additionally requires a real
    // neutral stick after such an overlap before it can emit a later edge.
    const bool currentUp = !current.cross() && verticalStickShotIntent(
        current, true, stickUpThreshold, lateralMaxRatio);
    const bool previousUp = !previous.cross() && verticalStickShotIntent(
        previous, true, stickUpThreshold, lateralMaxRatio);
    const bool currentDown = verticalStickShotIntent(
        current, false, stickDownThreshold, lateralMaxRatio);
    const bool previousDown = verticalStickShotIntent(
        previous, false, stickDownThreshold, lateralMaxRatio);

    ShotIntentEdges edges;
    edges.square = current.square() && !previous.square();
    edges.stickUp = currentUp && !previousUp;
    edges.stickDown = currentDown && !previousDown;
    return edges;
}

// Stateful first-edge detector for the controller -> reader wake/shot-epoch seam.
//
// AutomationEngine already requires three consecutive Square-UP polls before it
// treats a physical hold as released.  The reader wake path must use the same
// lifetime: if it used a raw previous/current comparison, one false-UP poll and
// the following still-held sample minted a second physical-shot epoch, reset the
// reader, and erased otherwise-valid ownership proof.  This tracker debounces
// same-gesture release without delaying the initial DOWN edge.
//
// Up <-> down is an explicit new right-stick gesture and remains an immediate
// cross-direction edge, matching the existing AutomationEngine latch semantics.
class ShotIntentEdgeTracker final {
public:
    static constexpr int kReleaseSamples = 3;
    // Once an OWNED Square release has been positively delivered, the console is
    // already observing Square-up.  A rapid human release/re-press can therefore
    // use two selected-device UP reports as its physical-end proof without
    // weakening the ordinary three-report debounce.  Two is deliberate: one
    // dropped RawInput/HID report remains incapable of minting a new shot epoch.
    static constexpr int kDeliveredSquareReleaseSamples = 2;

    [[nodiscard]] ShotIntentEdges update(
        const ControllerState& current,
        double stickUpThreshold,
        double stickDownThreshold,
        double lateralMaxRatio = 0.70) noexcept
    {
        squareEdgeEmittedThisUpdate_ = false;
        lastSquareEdgeUsedDeliveredRelease_ = false;
        const bool rawCurrentUp = verticalStickShotIntent(
            current, true, stickUpThreshold, lateralMaxRatio);
        const bool currentDown = verticalStickShotIntent(
            current, false, stickDownThreshold, lateralMaxRatio);
        const bool rightStickNeutral = std::hypot(
            static_cast<double>(current.rightStickX),
            static_cast<double>(current.rightStickY))
            < rawStickThreshold(stickUpThreshold);

        // A Cross/RS-up overlap is ambiguous gameplay input, not a Go-To shot.
        // Latch the suppression until a physically neutral right stick is seen;
        // otherwise releasing Cross while the same RS-up is still held would
        // manufacture a fresh shot edge one poll later.
        if (current.cross() && (rawCurrentUp || stickUpLatched_)) {
            stickUpBlockedUntilNeutral_ = true;
        }
        if (stickUpBlockedUntilNeutral_ && rightStickNeutral) {
            stickUpBlockedUntilNeutral_ = false;
        }
        const bool currentUp = rawCurrentUp && !current.cross()
            && !stickUpBlockedUntilNeutral_;

        // Preserve the established direct cross-direction gesture contract.
        if (currentUp) {
            releaseImmediately(stickDownLatched_, stickDownInactiveSamples_);
        } else if (currentDown) {
            releaseImmediately(stickUpLatched_, stickUpInactiveSamples_);
        }

        ShotIntentEdges edges;
        edges.square = updateSquareGesture(current.square());
        edges.stickUp = updateGesture(
            currentUp, stickUpLatched_, stickUpInactiveSamples_);
        edges.stickDown = updateGesture(
            currentDown, stickDownLatched_, stickDownInactiveSamples_);

        // A missing HID/RawInput transport report can never stand in for a
        // physical release. Suppress every edge until the reappeared selected
        // device proves all shot controls neutral for the normal three-sample
        // release lifetime. The third neutral report only clears the fence; a
        // later real DOWN/deflection is the first report allowed to emit.
        if (transportRecoveryAwaitingNeutral_) {
            if (shotInputControlsNeutral(
                    current, stickUpThreshold, stickDownThreshold)) {
                transportRecoveryNeutralSamples_ = std::min(
                    kReleaseSamples, transportRecoveryNeutralSamples_ + 1);
            } else {
                transportRecoveryNeutralSamples_ = 0;
            }
            if (transportRecoveryNeutralSamples_ >= kReleaseSamples) {
                transportRecoveryAwaitingNeutral_ = false;
                transportRecoveryNeutralSamples_ = 0;
            }
            return {};
        }
        return edges;
    }

    // Called only after the locally-authoritative controller route accepted a
    // generated Square-up release packet.  This grants no shot/fire authority;
    // it merely lets a subsequent REAL physical release/re-press form a fresh
    // input epoch after two UP reports.  The normal path remains three reports.
    //
    // Delivery confirmation is resolved later in the controller tick than the
    // raw-input update.  Preserve a just-observed two-UP -> DOWN rebound so the
    // confirmation can qualify it retroactively; the next still-DOWN poll emits
    // the edge.  A rebound from only one UP report remains latched.
    void noteOwnedSquareReleaseDelivered() noexcept
    {
        if (transportRecoveryAwaitingNeutral_ || squareEdgeEmittedThisUpdate_
            || !squareLatched_) {
            return;
        }
        deliveredSquareReleasePending_ = true;
        if (squareInactiveSamples_ >= kDeliveredSquareReleaseSamples
            || squareReboundInactiveSamples_ >= kDeliveredSquareReleaseSamples) {
            squareLatched_ = false;
            deliveredSquareReleaseQualified_ = true;
        }
    }

    // A queued completion from an older shot cannot grant the current gesture
    // the two-UP shortcut. Local delivery proves only the packet's own epoch;
    // the normal three-UP rearm remains available when identity is absent.
    void noteOwnedSquareReleaseDeliveredForEpoch(
        quint64 releasedPhysicalEpoch, quint64 currentPhysicalEpoch) noexcept
    {
        if (releasedPhysicalEpoch == 0 || releasedPhysicalEpoch != currentPhysicalEpoch) {
            return;
        }
        noteOwnedSquareReleaseDelivered();
    }

    [[nodiscard]] bool lastSquareEdgeUsedDeliveredRelease() const noexcept
    {
        return lastSquareEdgeUsedDeliveredRelease_;
    }

    // Entered only after a previously-live selected controller becomes
    // unavailable. Preserve every gesture latch: only update() calls carrying
    // real selected-device reports may prove the required physical neutral.
    void beginTransportRecovery() noexcept
    {
        transportRecoveryAwaitingNeutral_ = true;
        transportRecoveryNeutralSamples_ = 0;
        deliveredSquareReleasePending_ = false;
        deliveredSquareReleaseQualified_ = false;
        squareReboundInactiveSamples_ = 0;
    }

    [[nodiscard]] bool transportRecoveryActive() const noexcept
    {
        return transportRecoveryAwaitingNeutral_;
    }

    void reset() noexcept
    {
        squareLatched_ = false;
        stickUpLatched_ = false;
        stickDownLatched_ = false;
        stickUpBlockedUntilNeutral_ = false;
        squareInactiveSamples_ = kReleaseSamples;
        stickUpInactiveSamples_ = kReleaseSamples;
        stickDownInactiveSamples_ = kReleaseSamples;
        transportRecoveryAwaitingNeutral_ = false;
        transportRecoveryNeutralSamples_ = 0;
        deliveredSquareReleasePending_ = false;
        deliveredSquareReleaseQualified_ = false;
        squareReboundInactiveSamples_ = 0;
        squareEdgeEmittedThisUpdate_ = false;
        lastSquareEdgeUsedDeliveredRelease_ = false;
    }

private:
    [[nodiscard]] bool updateSquareGesture(bool active) noexcept
    {
        if (active) {
            // Retain only the immediately preceding inactive run.  This lets a
            // delivery acknowledgement later in THIS controller tick recover a
            // two-report rapid release, while an old mid-hold dropout cannot be
            // spent at some unrelated future release.
            squareReboundInactiveSamples_ = squareInactiveSamples_;
            squareInactiveSamples_ = 0;
            if (!squareLatched_) {
                squareLatched_ = true;
                squareEdgeEmittedThisUpdate_ = true;
                lastSquareEdgeUsedDeliveredRelease_ =
                    deliveredSquareReleaseQualified_;
                deliveredSquareReleasePending_ = false;
                deliveredSquareReleaseQualified_ = false;
                squareReboundInactiveSamples_ = 0;
                return true;
            }
            return false;
        }

        squareReboundInactiveSamples_ = 0;
        squareInactiveSamples_ = std::min(
            kReleaseSamples, squareInactiveSamples_ + 1);
        if (squareInactiveSamples_ >= kReleaseSamples) {
            // The ordinary fully-debounced boundary wins and carries no special
            // post-release attribution.
            squareLatched_ = false;
            deliveredSquareReleasePending_ = false;
            deliveredSquareReleaseQualified_ = false;
        } else if (deliveredSquareReleasePending_
                   && squareInactiveSamples_ >= kDeliveredSquareReleaseSamples) {
            squareLatched_ = false;
            deliveredSquareReleaseQualified_ = true;
        }
        return false;
    }

    [[nodiscard]] static bool updateGesture(
        bool active, bool& latched, int& inactiveSamples) noexcept
    {
        if (active) {
            inactiveSamples = 0;
            if (!latched) {
                latched = true;
                return true;
            }
            return false;
        }

        inactiveSamples = std::min(kReleaseSamples, inactiveSamples + 1);
        if (inactiveSamples >= kReleaseSamples) {
            latched = false;
        }
        return false;
    }

    static void releaseImmediately(bool& latched, int& inactiveSamples) noexcept
    {
        latched = false;
        inactiveSamples = kReleaseSamples;
    }

    bool squareLatched_ = false;
    bool stickUpLatched_ = false;
    bool stickDownLatched_ = false;
    bool stickUpBlockedUntilNeutral_ = false;
    int squareInactiveSamples_ = kReleaseSamples;
    int stickUpInactiveSamples_ = kReleaseSamples;
    int stickDownInactiveSamples_ = kReleaseSamples;
    bool transportRecoveryAwaitingNeutral_ = false;
    int transportRecoveryNeutralSamples_ = 0;
    bool deliveredSquareReleasePending_ = false;
    bool deliveredSquareReleaseQualified_ = false;
    int squareReboundInactiveSamples_ = 0;
    bool squareEdgeEmittedThisUpdate_ = false;
    bool lastSquareEdgeUsedDeliveredRelease_ = false;
};

// [DPAD-UP BYPASS HOTKEY 2026-08-08] Rising-edge latch for the physical D-pad Up
// meter-delay bypass-on-defense hotkey (owner-requested). A PASSIVE observer of
// the selected physical pad report: it never consumes, rewrites, or delays the
// report — D-pad Up keeps mirroring to the console for menu navigation — and it
// carries no shot or fire authority. The latch reuses the shot-intent release
// debounce (kReleaseSamples consecutive not-held polls, same as Square) so one
// dropped/false-UP poll mid-hold cannot mint a second toggle.
//
// `connected` gates EMISSION only, never the latch: a press held from the
// launcher stays latched across connect and must not fire retroactively; only a
// fresh physical press inside a connected session emits an edge. Symmetrically,
// a hold that survives a disconnect/reconnect never replays its edge.
class DpadUpEdgeTracker final {
public:
    static constexpr int kReleaseSamples = ShotIntentEdgeTracker::kReleaseSamples;

    // Returns true exactly once per physical press, and only when `connected`.
    [[nodiscard]] bool update(bool held, bool connected) noexcept
    {
        bool edge = false;
        if (held) {
            inactiveSamples_ = 0;
            if (!latched_) {
                latched_ = true;
                edge = true;
            }
        } else {
            inactiveSamples_ = std::min(kReleaseSamples, inactiveSamples_ + 1);
            if (inactiveSamples_ >= kReleaseSamples) {
                latched_ = false;
            }
        }
        return edge && connected;
    }

    void reset() noexcept
    {
        latched_ = false;
        inactiveSamples_ = kReleaseSamples;
    }

private:
    bool latched_ = false;
    int inactiveSamples_ = kReleaseSamples;
};

// ---------------------------------------------------------------------------
//  METER-GATE ARM AUTHORITY
//
//  Whether a physical shot edge may WAKE THE METER READER. This is deliberately
//  NOT the same question as "is the input session live".
//
//  [2026-08-26] The arm used to be gated on `streamActive` alone (remoteRunning_
//  || an embedded Chiaki surface). In CAPTURE-CARD mode the detector's frames
//  come from the HDMI capture card and have nothing to do with Chiaki, which is
//  input-only on that rig -- so a purely NETWORK failure benched VISION:
//
//    00:38:27  epoch=1 ... shot_gate_arm send: sent=1        <- worked
//    00:38:27  start_stream: Chiaki/input bring-up FAILED    <- console unreachable
//    00:38:27+ "Physical shot epoch" x30, NO arm send at all <- guard closed
//
//  RemotePlayState::Error is terminal for input authority and never retries, so
//  `streamActive` stayed false for the rest of the session. The reader's last
//  eligibility window (opened by epoch=1) expired 20s later and every frame
//  after it was rejected `gameplay_ineligible` -- 3859 of them -- while the
//  capture card ran at a healthy 60fps and the meter was plainly on screen.
//  Measured on that same footage, the reader detects 99.9% of frames when armed.
//
//  A failed promotion explicitly RESTORES the live preview
//  (RemotePlaySession::restoreWarmPreviewAfterPromotionFailure), so
//  `capturePreviewActive` is exactly the "we still have frames" signal the arm
//  should follow.
//
//  SAFETY: this edge carries NO fire authority. Release still requires the
//  scoped tokenized pose_arm, the server lease, and controller-route
//  attestation; and timing trust is held separately by the capture warm-cache
//  revocation. Widening this only lets the reader LOOK at a meter that is
//  already on screen -- which is the advertised behaviour of preview mode.
// ---------------------------------------------------------------------------
[[nodiscard]] inline bool meterGateArmAllowed(bool remoteRunning,
                                              bool chiakiEmbedded,
                                              bool capturePreviewActive,
                                              bool securityAllowed,
                                              unsigned int physicalShotEpoch) noexcept
{
    if (!securityAllowed || physicalShotEpoch == 0) {
        return false;
    }
    return remoteRunning || chiakiEmbedded || capturePreviewActive;
}

} // namespace orion
