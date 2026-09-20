#pragma once

#include <QtCore/QDateTime>
#include <QtCore/QString>

#include <cmath>

namespace orion {

// Two clocks describe a precise-fire transaction and they must never be
// conflated:
//   * commandIssuedMs is the scheduler event -- the instant immediately before
//     the active controller route is called.
//   * activeRouteCompleteMs is delivery telemetry -- for the direct pipe it is
//     sampled after the exact local-delivery ACK, and for ViGEm after submit()
//     returns.
//
// ACK/submit latency is deliberately observable in activeRouteDurationMs, but
// it cannot move commandIssuedMs (and therefore cannot move the release marker,
// scheduled-fire delta, or the latency learner's command-time anchor).
struct PreciseFireDispatchTiming final {
    double commandIssuedMs = -1.0;
    double activeRouteCompleteMs = -1.0;
    double activeRouteDurationMs = -1.0;

    [[nodiscard]] bool valid() const noexcept
    {
        return std::isfinite(commandIssuedMs) && commandIssuedMs >= 0.0
            && std::isfinite(activeRouteCompleteMs)
            && activeRouteCompleteMs >= commandIssuedMs
            && std::isfinite(activeRouteDurationMs)
            && activeRouteDurationMs >= 0.0;
    }
};

inline PreciseFireDispatchTiming finalizePreciseFireDispatchTiming(
    double commandIssuedMs, double activeRouteCompleteMs) noexcept
{
    PreciseFireDispatchTiming timing;
    if (!std::isfinite(commandIssuedMs) || commandIssuedMs < 0.0) {
        return timing;
    }
    timing.commandIssuedMs = commandIssuedMs;
    if (!std::isfinite(activeRouteCompleteMs)
        || activeRouteCompleteMs < commandIssuedMs) {
        return timing;
    }
    timing.activeRouteCompleteMs = activeRouteCompleteMs;
    timing.activeRouteDurationMs = activeRouteCompleteMs - commandIssuedMs;
    return timing;
}

// Keep the ordering itself in a pure, deterministic seam: sample the command
// clock, enter the active route exactly once, then sample delivery completion.
// Tests inject ACK delay through activeRouteDispatch; production supplies the
// real pipe/ViGEm call. No work may be inserted between the first sample and
// that call without changing this helper.
template<typename MonotonicNow, typename ActiveRouteDispatch>
inline PreciseFireDispatchTiming timePreciseFireDispatch(
    MonotonicNow monotonicNow, ActiveRouteDispatch activeRouteDispatch)
{
    const double commandIssuedMs = monotonicNow();
    activeRouteDispatch();
    return finalizePreciseFireDispatchTiming(
        commandIssuedMs, monotonicNow());
}

inline double alignFireEpochMs(double fireMonotonicMs,
                               double monotonicBeforeWallMs,
                               qint64 wallEpochMs,
                               double monotonicAfterWallMs) noexcept
{
    if (!std::isfinite(fireMonotonicMs)
        || !std::isfinite(monotonicBeforeWallMs)
        || !std::isfinite(monotonicAfterWallMs)
        || fireMonotonicMs < 0.0
        || monotonicBeforeWallMs < 0.0
        || monotonicAfterWallMs < monotonicBeforeWallMs
        || wallEpochMs <= 0) {
        return -1.0;
    }
    const double monotonicAtWallMs = monotonicBeforeWallMs
        + (monotonicAfterWallMs - monotonicBeforeWallMs) * 0.5;
    return static_cast<double>(wallEpochMs)
        + fireMonotonicMs - monotonicAtWallMs;
}

template<typename MonotonicNow>
inline double captureFireEpochMs(double fireMonotonicMs, MonotonicNow monotonicNow)
{
    const double monotonicBeforeWallMs = monotonicNow();
    const qint64 wallEpochMs = QDateTime::currentMSecsSinceEpoch();
    const double monotonicAfterWallMs = monotonicNow();
    return alignFireEpochMs(fireMonotonicMs,
                            monotonicBeforeWallMs,
                            wallEpochMs,
                            monotonicAfterWallMs);
}

inline QString fireEpochLogField(double fireEpochMs)
{
    const double value = std::isfinite(fireEpochMs) && fireEpochMs > 0.0
        ? fireEpochMs : -1.0;
    return QStringLiteral("fire_epoch_ms=%1").arg(value, 0, 'f', 3);
}

} // namespace orion
