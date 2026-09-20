#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

namespace orion::fade_phase_catchup {

// Detect one discrete forward progression jump, NOT gradual speed drift. Inputs must
// already be genuine same-shot observations. Reference offsets come from the engine's
// existing curve; this helper neither fits a new curve nor reads a shot outcome.
struct Observation {
    double captureMs = -1.0;
    double fillPct = -1.0;
    double coarsePct = -1.0;
    double referenceMs = 0.0;
    double coarseReferenceMs = 0.0;
    int frame = -1;
    std::uint64_t ruler = 0;
    int x = 0, y = 0, width = 0, height = 0;
};

class Estimator final {
public:
    // A break clears observations, never the once-per-shot attempt latch. ShotContext's
    // lifetime resets the whole object. Repeated rejected boxes cannot seed a new clock.
    void clearEvidence() noexcept { havePrevious_ = false; count_ = 0; }
    [[nodiscard]] bool attempted() const noexcept { return attempted_; }

    // Zero means retain the existing deadline. A negative result is a PROPOSAL only;
    // the engine/worker transaction still owns identity, receipt-time runway and commit.
    double observe(const Observation& o, double anchor20Ms, double periodMs) noexcept
    {
        if (attempted_) return 0.0;
        if (!valid(o) || !std::isfinite(anchor20Ms) || anchor20Ms < 0.0
            || !std::isfinite(periodMs) || periodMs < 10.0 || periodMs > 25.0) {
            clearEvidence();
            return 0.0;
        }
        const double innovation = o.captureMs - o.referenceMs - anchor20Ms;
        const double dt = o.captureMs - previous_.captureMs;
        const bool continuous = havePrevious_ && std::abs(anchor20Ms - anchor20Ms_) < 1e-6
            && o.ruler == previous_.ruler
            && static_cast<std::int64_t>(o.frame) == static_cast<std::int64_t>(previous_.frame) + 1
            && dt >= 0.65 * periodMs && dt <= 1.35 * periodMs
            && geometryContinuous(previous_, o);
        if (!continuous) count_ = 0;
        double correction = 0.0;
        if (continuous) {
            const double step = o.referenceMs - previous_.referenceMs;
            const double coarseStep = o.coarseReferenceMs - previous_.coarseReferenceMs;
            const double previousInnovation = previous_.captureMs - previous_.referenceMs - anchor20Ms;
            const bool negativeBounded = innovation <= -periodMs && innovation >= -2.5 * periodMs;
            if (count_ == 0) {
                // Both exposed pixel rulers must show the extra 1-2 frames.
                // A shifted box or only one over-read is not a progression jump.
                const double excess = step - dt;
                const double coarseExcess = coarseStep - dt;
                if (o.fillPct <= 40.0 && std::abs(previousInnovation) <= 0.75 * periodMs
                    && excess >= 1.25 * periodMs && excess <= 2.5 * periodMs
                    && coarseExcess >= 1.25 * periodMs && coarseExcess <= 2.5 * periodMs
                    && std::abs(excess - coarseExcess) <= 0.5 * periodMs && negativeBounded) {
                    innovations_[0] = innovation;
                    count_ = 1;
                }
            } else if (negativeBounded && step >= 0.65 * dt && step <= 1.35 * dt
                       && coarseStep >= 0.4 * dt && coarseStep <= 1.8 * dt) {
                innovations_[count_++] = innovation;
                const auto end = innovations_.begin() + count_;
                const auto range = std::minmax_element(innovations_.begin(), end);
                if (*range.second - *range.first > 0.35 * periodMs) {
                    count_ = 0;
                } else if (count_ == 3) {
                    auto ordered = innovations_;
                    std::sort(ordered.begin(), ordered.end());
                    correction = ordered[1];
                    attempted_ = true;
                }
            } else {
                count_ = 0;
            }
        }
        previous_ = o;
        anchor20Ms_ = anchor20Ms;
        havePrevious_ = true;
        return correction;
    }

private:
    static bool valid(const Observation& o) noexcept
    {
        return std::isfinite(o.captureMs) && o.captureMs >= 0.0
            && std::isfinite(o.fillPct) && o.fillPct > 0.0 && o.fillPct <= 50.0
            && std::isfinite(o.coarsePct) && o.coarsePct > 0.0 && o.coarsePct <= 53.0
            && std::abs(o.fillPct - o.coarsePct) <= 3.0
            && std::isfinite(o.referenceMs) && std::isfinite(o.coarseReferenceMs)
            && o.frame >= 0 && o.ruler != 0 && o.width > 0 && o.height > 0;
    }
    static bool geometryContinuous(const Observation& a, const Observation& b) noexcept
    {
        return std::abs(double(b.width) - a.width) <= 0.10 * a.width
            && std::abs(double(b.height) - a.height) <= 0.06 * a.height
            && std::abs(double(b.x) - a.x) <= 0.50 * a.width
            && std::abs(double(b.y) - a.y) <= 0.15 * a.height;
    }
    Observation previous_;
    std::array<double, 3> innovations_{};
    double anchor20Ms_ = -1.0;
    int count_ = 0;
    bool havePrevious_ = false;
    bool attempted_ = false;
};

} // namespace orion::fade_phase_catchup
