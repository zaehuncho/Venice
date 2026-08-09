#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace orion::live_hud {

// These helpers keep the launcher's live-HUD truth rules independent from QML
// repaint cadence. They intentionally contain no detector or timing authority:
// the controller supplies an already-authoritative sample and these functions
// only decide whether it is still honest to display.
inline bool meterMetricFresh(bool current,
                             std::int64_t observedAtMs,
                             std::int64_t nowMs,
                             std::int64_t freshnessMs) noexcept
{
    if (!current || observedAtMs <= 0 || freshnessMs <= 0) {
        return false;
    }
    const std::int64_t ageMs = nowMs - observedAtMs;
    return ageMs >= 0 && ageMs < freshnessMs;
}

// Duration of native bot ownership only. The physical input edge deliberately
// is not an argument: ownership starts when Orion commits the shot transition.
inline double botOwnedDurationMs(std::uint64_t activeArmToken,
                                 std::uint64_t ownershipArmToken,
                                 double ownershipStartedMs,
                                 double releaseTriggeredMs,
                                 double nowMs) noexcept
{
    if (activeArmToken == 0 || activeArmToken != ownershipArmToken
        || !std::isfinite(ownershipStartedMs) || ownershipStartedMs < 0.0
        || !std::isfinite(releaseTriggeredMs)) {
        return -1.0;
    }
    const double endMs = releaseTriggeredMs >= 0.0 ? releaseTriggeredMs : nowMs;
    if (!std::isfinite(endMs) || endMs < ownershipStartedMs) {
        return -1.0;
    }
    return endMs - ownershipStartedMs;
}

// A displayed ETA must be from the same arm token and the same target that the
// bot currently owns. This explicitly prevents a green-centre or registration
// ETA from being relabelled as an ETA to a different active target. The source
// value is captured at the metric observation; age is subtracted at read time.
inline double matchingActiveTargetEtaMs(bool metricCurrent,
                                        std::int64_t metricObservedAtMs,
                                        std::int64_t nowMs,
                                        std::int64_t freshnessMs,
                                        std::uint64_t activeArmToken,
                                        std::uint64_t etaArmToken,
                                        double activeTargetPct,
                                        double etaTargetPct,
                                        double etaAtObservationMs) noexcept
{
    constexpr double kTargetMatchTolerancePct = 0.01;
    if (!meterMetricFresh(metricCurrent, metricObservedAtMs, nowMs, freshnessMs)
        || activeArmToken == 0 || activeArmToken != etaArmToken
        || !std::isfinite(activeTargetPct) || !std::isfinite(etaTargetPct)
        || std::abs(activeTargetPct - etaTargetPct) > kTargetMatchTolerancePct
        || !std::isfinite(etaAtObservationMs) || etaAtObservationMs < 0.0) {
        return -1.0;
    }
    const double ageMs = static_cast<double>(nowMs - metricObservedAtMs);
    return std::max(0.0, etaAtObservationMs - ageMs);
}

} // namespace orion::live_hud
