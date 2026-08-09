#pragma once

#include "OrionTypes.h"

#include <cmath>
#include <cstdint>

namespace orion {

// Pure policy seam for translating AutomationEngine's absolute monotonic
// deadlines into the precise-fire worker's steady-clock domain.  A negative
// authority expiry is the one explicit "unbounded" sentinel; an expired,
// non-finite, or too-short finite lease must never be laundered into it.
struct PreciseFireArmWindow final {
    bool allowed = false;
    bool finiteAuthority = false;
    double delayMs = 0.0;
    double authorityRemainingMs = -1.0;
};

// [ORION_ROLLING_LEASE] Decision seam for handing the engine's rolling meter-authority lease to
// an ALREADY-ARMED token. arm() copies the lease once and the worker then ran to its deadline on
// that frozen copy, so a single dropped frame near the deadline expired evidence the engine had
// already renewed, and the worker silently declined to submit.
//
// Kept pure and separate from evaluate() because its safety properties are the whole point and
// have to be testable without a thread: it may only ever EXTEND, it must refuse an already-
// expired engine lease rather than resurrect one, and it must never launder a finite lease into
// the unbounded sentinel. It deliberately says nothing about the fire deadline -- this governs
// WHETHER a proven-future token may submit, never WHEN it fires.
struct PreciseFireAuthorityRefresh final {
    bool extend = false;
    double remainingMs = -1.0;
};

// A direct-pipe send distinguishes a real write from a de-dup hit and from a
// route failure. The legacy bool OrionInputClient::send API keeps its historical
// "true only when bytes were written" contract; the precise worker uses this
// richer result because Unchanged is NOT proof that this token produced a
// hold->release edge at its deadline (the same released state may have been
// latched early or without a preceding held packet).
enum class InputRouteWriteResult {
    Failed,
    Unchanged,
    // Ordinary latest-wins packet was written; no delivery ACK was requested.
    Written,
    // Exact flagged transaction was accepted by OrionStream's local UDP socket.
    // This is not a console/game acknowledgement.
    LocalUdpAccepted,
    // Ordered own=0 barrier completed after all prior Orion transactions.
    OwnershipReleased,
    // Bytes may have reached OrionStream, but the exact downstream ACK was not received.
    // The route is ambiguous and must not race an immediate virtual-pad fallback.
    WrittenUnconfirmed,
};

// Stable parser-facing delivery vocabulary used by the precise-fire mailbox and Release submit
// telemetry. Both stages are local acceptance only; neither is a console/game acknowledgement.
enum class PreciseFireDeliveryStage {
    None,
    LocalUdpAccepted,
    ActiveVigemSubmit,
};

// Immutable controller-route identity copied into every scheduled-fire token.
// Availability is not identity: a pipe reconnect or ViGEm fallback may become
// usable while a deadline is waiting, but it may not inherit a token whose
// latency was calculated for another route/generation.
struct PreciseFireRouteBinding final {
    std::uint64_t generation = 0;
    LatencyControllerRoute route = LatencyControllerRoute::None;

    [[nodiscard]] constexpr bool valid() const noexcept
    {
        return generation != 0 && route != LatencyControllerRoute::None;
    }
};

[[nodiscard]] static constexpr const char* preciseFireDeliveryStageField(
    PreciseFireDeliveryStage stage) noexcept
{
    switch (stage) {
    case PreciseFireDeliveryStage::LocalUdpAccepted:
        return "local_udp_accepted";
    case PreciseFireDeliveryStage::ActiveVigemSubmit:
        return "active_vigem_submit";
    case PreciseFireDeliveryStage::None:
        return "not_confirmed";
    }
    return "not_confirmed";
}

[[nodiscard]] static constexpr InputRouteWriteResult inputRouteWriteFailureResult(
    bool routeWasOwned, bool writeMayHaveBeenAccepted) noexcept
{
    // A pending message can complete concurrently with timeout/cancellation. Treat that state as
    // ambiguous even on the first owned packet so ViGEm never races a packet Chiaki may have read.
    return routeWasOwned || writeMayHaveBeenAccepted
        ? InputRouteWriteResult::WrittenUnconfirmed
        : InputRouteWriteResult::Failed;
}

[[nodiscard]] static constexpr bool inputRouteMayStillOwnInput(
    InputRouteWriteResult result) noexcept
{
    return result == InputRouteWriteResult::Written
        || result == InputRouteWriteResult::LocalUdpAccepted
        || result == InputRouteWriteResult::WrittenUnconfirmed;
}

struct PreciseFireDeliveryDecision final {
    bool pipeAccepted = false;
    bool virtualFallbackAccepted = false;
    bool confirmScheduledFire = false;
    bool controllerFault = true;
    PreciseFireDeliveryStage stage = PreciseFireDeliveryStage::None;
};

struct PreciseFirePolicy final {
    [[nodiscard]] static constexpr LatencyControllerRoute liveControllerRoute(
        bool sessionRunning, bool inputHookEnabled, bool inputHookConnected,
        bool virtualControllerConnected, bool virtualControllerIsDs4) noexcept
    {
        if (!sessionRunning || !virtualControllerConnected) {
            return LatencyControllerRoute::None;
        }
        if (inputHookEnabled && inputHookConnected) {
            return LatencyControllerRoute::Pipe;
        }
        return virtualControllerIsDs4
            ? LatencyControllerRoute::VigemDs4
            : LatencyControllerRoute::VigemXusb;
    }

    [[nodiscard]] static constexpr bool routeBindingMatches(
        const PreciseFireRouteBinding& binding,
        LatencyControllerRoute liveRoute,
        std::uint64_t currentGeneration,
        LatencyControllerRoute currentRoute) noexcept
    {
        return binding.valid() && liveRoute != LatencyControllerRoute::None
            && binding.generation == currentGeneration
            && binding.route == currentRoute
            && binding.route == liveRoute;
    }

    [[nodiscard]] static constexpr bool controllerStateFullyNeutral(
        const ControllerState& state) noexcept
    {
        return state.buttons == 0 && state.dpad == 8
            && state.leftStickX == 0 && state.leftStickY == 0
            && state.rightStickX == 0 && state.rightStickY == 0
            && state.l2 == 0 && state.r2 == 0 && !state.touchpad;
    }

    [[nodiscard]] static constexpr bool latencyRouteAttestationEligible(
        bool sessionRunning, bool controllerConnected, bool automationArmed,
        bool routeFaultFree, const ControllerState& physical,
        const ControllerState& output) noexcept
    {
        return sessionRunning && controllerConnected && automationArmed && routeFaultFree
            && controllerStateFullyNeutral(physical)
            && controllerStateFullyNeutral(output);
    }
    [[nodiscard]] static constexpr bool hasConfirmedPreciseDelivery(
        PreciseFireDeliveryStage stage, std::uint64_t fireToken) noexcept
    {
        return stage != PreciseFireDeliveryStage::None && fireToken != 0;
    }

    // An immediate engine-tick release needs the same exact local delivery
    // acknowledgement as a sub-tick worker release. Ordinary mirror frames may
    // use latest-wins/de-dup semantics, but a pending release or recovery proof
    // must force a distinct transaction that can be paired to its release seq.
    [[nodiscard]] static constexpr bool requiresExactDeliveryAck(
        bool pendingRelease,
        bool preciseReleaseAlreadyDelivered,
        bool routeRecoveryProbe) noexcept
    {
        return (pendingRelease && !preciseReleaseAlreadyDelivered)
            || routeRecoveryProbe;
    }

    // Critical releases and route-recovery probes must bypass the ordinary
    // mirror-frame coalescing window. Otherwise a valid tip decision that
    // lands shortly after the previous hook write can be silently skipped.
    [[nodiscard]] static constexpr bool shouldAttemptDirectWrite(
        bool directInputReady,
        bool exactDeliveryAckRequired,
        bool suppressDeliveredPreciseDuplicate,
        std::int64_t elapsedSinceLastWriteUs,
        std::int64_t coalesceWindowUs) noexcept
    {
        return directInputReady
            && (exactDeliveryAckRequired
                || (!suppressDeliveredPreciseDuplicate
                    && elapsedSinceLastWriteUs >= coalesceWindowUs));
    }

    [[nodiscard]] static PreciseFireArmWindow evaluate(
        double absoluteDeadlineMs,
        double absoluteAuthorityExpiryMs,
        double engineNowMs) noexcept
    {
        PreciseFireArmWindow out;
        if (!std::isfinite(absoluteDeadlineMs) || !std::isfinite(engineNowMs)
            || absoluteDeadlineMs < 0.0) {
            return out;
        }

        out.delayMs = absoluteDeadlineMs > engineNowMs
            ? absoluteDeadlineMs - engineNowMs : 0.0;
        if (absoluteAuthorityExpiryMs < 0.0) {
            out.allowed = true;
            return out;
        }
        if (!std::isfinite(absoluteAuthorityExpiryMs)) {
            return out;
        }

        out.finiteAuthority = true;
        out.authorityRemainingMs = absoluteAuthorityExpiryMs - engineNowMs;
        // Equality is already expired: the worker cannot submit before "now".
        // Also reject a tick-nudged deadline that extends beyond its evidence.
        out.allowed = out.authorityRemainingMs > 0.0
            && out.delayMs <= out.authorityRemainingMs;
        return out;
    }

    // See PreciseFireAuthorityRefresh. `currentRemainingMs` is the armed token's EXISTING lease
    // measured from the same instant as engineNowMs; pass +infinity for the unbounded sentinel so
    // the monotonic test below refuses to shorten it.
    [[nodiscard]] static PreciseFireAuthorityRefresh evaluateAuthorityRefresh(
        double absoluteAuthorityExpiryMs,
        double engineNowMs,
        double currentRemainingMs,
        bool armed,
        bool tokenMatches) noexcept
    {
        PreciseFireAuthorityRefresh out;
        if (!armed || !tokenMatches) {
            return out;
        }
        // A negative expiry is the engine's "no finite authority" sentinel. Refusing it here is
        // what keeps a finite lease finite: an armed token must never be promoted to unbounded
        // evidence by a later tick.
        if (!std::isfinite(absoluteAuthorityExpiryMs) || absoluteAuthorityExpiryMs < 0.0
            || !std::isfinite(engineNowMs)) {
            return out;
        }
        const double remainingMs = absoluteAuthorityExpiryMs - engineNowMs;
        // Expired (or exactly at zero) is refused rather than propagated: the point is to carry
        // freshness the engine still has, never to revive evidence that has genuinely gone stale.
        if (remainingMs <= 0.0) {
            return out;
        }
        out.remainingMs = remainingMs;
        // MONOTONIC. NaN in currentRemainingMs makes this false, which fails safe (no extend).
        out.extend = remainingMs > currentRemainingMs;
        return out;
    }

    // The desktop ViGEm target is deliberately submitted NEUTRAL while the
    // direct Chiaki pipe owns PS5 input. A successful neutral desktop submit
    // therefore cannot count as release delivery. ViGEm counts only when it is
    // the active fallback carrying releaseOutput; the pipe counts only when
    // this deadline received exact local UDP acceptance. Unchanged and plain Written deliberately
    // fail because neither proves the critical transaction reached the local socket.
    [[nodiscard]] static constexpr PreciseFireDeliveryDecision evaluateDelivery(
        InputRouteWriteResult pipeWrite,
        bool directPipeOwnsInput,
        bool virtualSubmitOk) noexcept
    {
        PreciseFireDeliveryDecision out;
        out.pipeAccepted = directPipeOwnsInput
            && pipeWrite == InputRouteWriteResult::LocalUdpAccepted;
        out.virtualFallbackAccepted = !directPipeOwnsInput && virtualSubmitOk;
        out.confirmScheduledFire = out.pipeAccepted || out.virtualFallbackAccepted;
        out.controllerFault = !out.confirmScheduledFire;
        out.stage = out.pipeAccepted
            ? PreciseFireDeliveryStage::LocalUdpAccepted
            : out.virtualFallbackAccepted
                ? PreciseFireDeliveryStage::ActiveVigemSubmit
                : PreciseFireDeliveryStage::None;
        return out;
    }

    // A scheduled token may be delivered only by its immutable route. In
    // particular, a failed Pipe token cannot be rescued by a successful ViGEm
    // write, and a newly connected Pipe cannot steal a ViGEm token.
    [[nodiscard]] static constexpr PreciseFireDeliveryDecision evaluateBoundDelivery(
        InputRouteWriteResult pipeWrite,
        bool virtualSubmitOk,
        LatencyControllerRoute boundRoute) noexcept
    {
        PreciseFireDeliveryDecision out;
        out.pipeAccepted = boundRoute == LatencyControllerRoute::Pipe
            && pipeWrite == InputRouteWriteResult::LocalUdpAccepted;
        out.virtualFallbackAccepted =
            (boundRoute == LatencyControllerRoute::VigemDs4
             || boundRoute == LatencyControllerRoute::VigemXusb)
            && virtualSubmitOk;
        out.confirmScheduledFire = out.pipeAccepted || out.virtualFallbackAccepted;
        out.controllerFault = !out.confirmScheduledFire;
        out.stage = out.pipeAccepted
            ? PreciseFireDeliveryStage::LocalUdpAccepted
            : out.virtualFallbackAccepted
                ? PreciseFireDeliveryStage::ActiveVigemSubmit
                : PreciseFireDeliveryStage::None;
        return out;
    }

    // Recovery is intentionally stricter than "the handle exists": an active
    // route must accept a packet, and the physical shot gesture must remain
    // neutral for a short stable run so AutomationEngine::reset cannot re-arm a
    // still-held Square/RS gesture as a second takeover.
    [[nodiscard]] static constexpr bool routeRecoveryReady(
        bool activeRouteAccepted,
        bool physicalShotInputsNeutral,
        int consecutiveNeutralFrames,
        int requiredNeutralFrames = 3) noexcept
    {
        return activeRouteAccepted && physicalShotInputsNeutral
            && requiredNeutralFrames > 0
            && consecutiveNeutralFrames >= requiredNeutralFrames;
    }
};

} // namespace orion
