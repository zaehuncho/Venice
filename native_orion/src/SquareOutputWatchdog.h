#pragma once

#include <cstdint>

namespace orion {

// Observes confirmed OUTPUT, never raw input as a proxy for delivery. A shot
// or intentional abort drain remains ownership: timing out that drain would
// fabricate the blind Square-up edge its false-shot gate prevents.
class SquareOutputWatchdog final {
public:
    static constexpr std::int64_t kUnownedHoldLimitMs = 1500;
    void observeOutput(bool held, std::int64_t nowMs) noexcept
    {
        if (!held) heldSinceMs_ = -1;
        else if (heldSinceMs_ < 0 || nowMs < heldSinceMs_) heldSinceMs_ = nowMs;
    }
    [[nodiscard]] bool releaseDue(std::int64_t nowMs, bool engineOwnsOutput) const noexcept
    {
        return !engineOwnsOutput && !suppressUntilPhysicalEnd_
            && heldSinceMs_ >= 0 && nowMs >= heldSinceMs_
            && nowMs - heldSinceMs_ >= kUnownedHoldLimitMs;
    }
    void noteReleaseAttempt() noexcept
    {
        heldSinceMs_ = -1;
        suppressUntilPhysicalEnd_ = true;
        physicalUpSamples_ = 0;
    }
    // Absence is not release. Match the shot-intent path's three real UP polls.
    void observePhysical(bool present, bool held) noexcept
    {
        if (!suppressUntilPhysicalEnd_) return;
        if (!present || held) physicalUpSamples_ = 0;
        else if (++physicalUpSamples_ >= 3) {
            suppressUntilPhysicalEnd_ = false;
            physicalUpSamples_ = 0;
        }
    }
    [[nodiscard]] bool suppressSquare() const noexcept { return suppressUntilPhysicalEnd_; }
private:
    std::int64_t heldSinceMs_ = -1;
    int physicalUpSamples_ = 0;
    bool suppressUntilPhysicalEnd_ = false;
};

} // namespace orion
