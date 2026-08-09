#pragma once

#include "OrionTypes.h"

#include <cstdint>
#include <limits>

namespace orion {

// Process-local state machine for binding a restored sidecar latency snapshot to the
// controller route that native actually accepted. A neutral delivery creates one pending
// generation. Repeated controller ticks cannot replace that generation while its echo is in
// flight, and only the exact sidecar generation+route pair can confirm it.
class LatencyRouteAttestationHandshake final {
public:
    enum class Phase : uint8_t {
        Empty,
        Pending,
        Confirmed,
    };

    // Sidecar telemetry normally returns in one or two video frames. This leaves ample room for
    // transient scheduling jitter while still bounding a lost command or dead source epoch.
    static constexpr qint64 kPendingTimeoutMs = 1500;

    [[nodiscard]] bool beginPending(
        LatencyControllerRoute route, qint64 issuedAtMs) noexcept
    {
        if (phase_ != Phase::Empty || route == LatencyControllerRoute::None
            || issuedAtMs < 0) {
            return false;
        }

        ++lastIssuedGeneration_;
        if (lastIssuedGeneration_ == 0) {
            ++lastIssuedGeneration_; // zero is the invalid wire sentinel
        }
        generation_ = lastIssuedGeneration_;
        route_ = route;
        scopeEpoch_ = observedScopeEpoch_;
        issuedAtMs_ = issuedAtMs;
        retryCount_ = 0;
        phase_ = Phase::Pending;
        return true;
    }

    [[nodiscard]] bool completeExactEcho(
        quint64 generation, LatencyControllerRoute route,
        quint64 scopeEpoch) noexcept
    {
        if (phase_ != Phase::Pending || generation == 0
            || generation != generation_ || route == LatencyControllerRoute::None
            || route != route_ || scopeEpoch == 0
            || (observedScopeEpoch_ != 0 && scopeEpoch != observedScopeEpoch_)
            || (scopeEpoch_ != 0 && scopeEpoch != scopeEpoch_)) {
            return false;
        }
        observedScopeEpoch_ = scopeEpoch;
        scopeEpoch_ = scopeEpoch;
        phase_ = Phase::Confirmed;
        issuedAtMs_ = -1;
        return true;
    }

    // The sidecar increments this process-local epoch whenever capture source/mode
    // replaces the estimator, even if its route text remains identical. A changed
    // nonzero epoch therefore revokes an in-flight or confirmed proof exactly once.
    // Zero/missing cold telemetry is observational and must not create 60 Hz churn.
    [[nodiscard]] bool observeScopeEpoch(quint64 scopeEpoch) noexcept
    {
        if (scopeEpoch == 0) {
            return false;
        }
        if (observedScopeEpoch_ == 0) {
            observedScopeEpoch_ = scopeEpoch;
            if (active() && scopeEpoch_ == 0) {
                scopeEpoch_ = scopeEpoch;
            }
            return false;
        }
        if (scopeEpoch == observedScopeEpoch_) {
            return false;
        }

        // Epochs are process-monotonic. A regression is stale/corrupt input: fail
        // closed without rolling the high-water mark backward. An advance becomes
        // the baseline for the one replacement proof minted on the next neutral tick.
        const bool hadActiveProof = active();
        if (scopeEpoch > observedScopeEpoch_) {
            observedScopeEpoch_ = scopeEpoch;
        }
        if (hadActiveProof) {
            revoke();
        }
        return hadActiveProof;
    }

    [[nodiscard]] bool revokePendingIfTimedOut(
        qint64 nowMs, qint64 timeoutMs = kPendingTimeoutMs) noexcept
    {
        if (phase_ != Phase::Pending || issuedAtMs_ < 0 || timeoutMs <= 0
            || nowMs < issuedAtMs_ || nowMs - issuedAtMs_ < timeoutMs) {
            return false;
        }
        revoke();
        return true;
    }

    // A lost stdout receipt is retried with the SAME generation. Minting a replacement token on
    // every timeout creates a moving target for delayed sidecar work and can keep an otherwise
    // healthy route permanently unauthorised. Explicit route/source/runtime transitions still use
    // revoke(), which is the only operation allowed to discard this generation.
    [[nodiscard]] bool retryPendingIfTimedOut(
        qint64 nowMs, qint64 timeoutMs = kPendingTimeoutMs) noexcept
    {
        if (phase_ != Phase::Pending || issuedAtMs_ < 0 || timeoutMs <= 0
            || nowMs < issuedAtMs_ || nowMs - issuedAtMs_ < timeoutMs) {
            return false;
        }
        issuedAtMs_ = nowMs;
        if (retryCount_ != std::numeric_limits<unsigned int>::max()) {
            ++retryCount_;
        }
        return true;
    }

    // Revocation deliberately preserves lastIssuedGeneration_. A late echo from a reset source
    // must never equal the next proof minted in this process.
    void revoke() noexcept
    {
        phase_ = Phase::Empty;
        generation_ = 0;
        route_ = LatencyControllerRoute::None;
        scopeEpoch_ = 0;
        issuedAtMs_ = -1;
        retryCount_ = 0;
    }

    // Scope epochs are monotonic only within one sidecar process. A confirmed
    // QProcess::started edge establishes a new epoch namespace, so discard the
    // prior process's epoch high-water mark while preserving
    // lastIssuedGeneration_. Keeping the native generation monotonic still
    // fences every delayed acknowledgement from an older process.
    void beginSidecarProcessGeneration() noexcept
    {
        revoke();
        observedScopeEpoch_ = 0;
    }

    [[nodiscard]] Phase phase() const noexcept { return phase_; }
    [[nodiscard]] bool active() const noexcept { return phase_ != Phase::Empty; }
    [[nodiscard]] bool pending() const noexcept { return phase_ == Phase::Pending; }
    [[nodiscard]] bool confirmed() const noexcept { return phase_ == Phase::Confirmed; }
    [[nodiscard]] bool needsNeutralProof() const noexcept { return phase_ == Phase::Empty; }
    [[nodiscard]] quint64 generation() const noexcept { return generation_; }
    [[nodiscard]] LatencyControllerRoute route() const noexcept { return route_; }
    [[nodiscard]] quint64 scopeEpoch() const noexcept { return scopeEpoch_; }
    [[nodiscard]] quint64 observedScopeEpoch() const noexcept
    {
        return observedScopeEpoch_;
    }
    [[nodiscard]] qint64 issuedAtMs() const noexcept { return issuedAtMs_; }
    [[nodiscard]] unsigned int retryCount() const noexcept { return retryCount_; }
    [[nodiscard]] bool matchesActiveRoute(LatencyControllerRoute route) const noexcept
    {
        return active() && route != LatencyControllerRoute::None && route_ == route;
    }

private:
    Phase phase_ = Phase::Empty;
    quint64 lastIssuedGeneration_ = 0;
    quint64 generation_ = 0;
    LatencyControllerRoute route_ = LatencyControllerRoute::None;
    quint64 observedScopeEpoch_ = 0;
    quint64 scopeEpoch_ = 0;
    qint64 issuedAtMs_ = -1;
    unsigned int retryCount_ = 0;
};

} // namespace orion
