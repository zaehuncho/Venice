#pragma once

#include <cmath>

namespace orion {

// A learned feedforward clock is only a backstop. When current detector
// evidence describes a credible forward target crossing, vision owns timing
// even if its latency-compensated fire instant is already due/past. Requiring
// the fire instant itself to be future is the late-acquisition hole: the raw
// clock wins precisely when vision says the release should happen now.
[[nodiscard]] inline bool healthyForwardCrossingOwnsTiming(
    bool genuineCurrentFrame,
    int freshAcceptsWithin150Ms,
    double velocityPctPerMs,
    double fillPct,
    double targetPct,
    double crossingMs,
    double nowMs,
    double effectiveLatencyMs,
    double visionFireEtaCapMs) noexcept
{
    if (!genuineCurrentFrame || freshAcceptsWithin150Ms < 3
        || !std::isfinite(velocityPctPerMs) || velocityPctPerMs <= 0.02
        || !std::isfinite(fillPct) || !std::isfinite(targetPct)
        || fillPct < 0.0 || fillPct >= targetPct
        || !std::isfinite(crossingMs) || !std::isfinite(nowMs)
        || !std::isfinite(effectiveLatencyMs) || effectiveLatencyMs < 0.0
        || !std::isfinite(visionFireEtaCapMs) || visionFireEtaCapMs <= 0.0) {
        return false;
    }

    const double crossingEtaMs = crossingMs - nowMs;
    if (crossingEtaMs <= 0.0) {
        return false;
    }

    const double visionFireAtMs = crossingMs - effectiveLatencyMs;
    return std::isfinite(visionFireAtMs)
        && visionFireAtMs <= nowMs + visionFireEtaCapMs;
}

} // namespace orion
