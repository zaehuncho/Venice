#pragma once

#include <QtGui/QImage>

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <utility>

namespace orion {

// Small display-only jitter buffer for the live preview.
//
// Capture/detection never passes through this class. It only paces already-owned
// preview QImages before they reach QML. Keeping frameNumber beside the image is
// part of the invariant: an overflow drops both together, so the overlay can
// never be joined to a different frame after smoothing.
class PreviewPresentationBuffer final {
public:
    struct Frame final {
        QImage image;
        int frameNumber = -1;
    };

    struct PushResult final {
        bool accepted = false;
        bool droppedOldest = false;
        std::size_t depth = 0;
    };

    // Live capture telemetry shows ordinary 0/32 ms delivery bursts plus an
    // occasional 47 ms Windows scheduling gap even while the SHM producer is
    // healthy at ~60 FPS. A two-frame cushion repeatedly drained on that input;
    // the presenter's subsequent re-prime amplified the source gap into a
    // visible 40-62 ms hold. Three queued frames absorb a single 47 ms burst.
    // The fourth slot bounds the catch-up race without allowing display latency
    // to grow without limit. This queue is presentation-only: detector/timing
    // frames never enter it.
    static constexpr std::size_t kPrimeDepth = 3;
    // After a rare true empty, rebuild one frame of reserve without imposing a
    // second full initial prime. With the absolute phase retained, the next
    // presentation is bounded to two recovery ticks (at most three periods
    // from the last displayed frame, including the empty tick).
    static constexpr std::size_t kRecoveryDepth = 2;
    static constexpr std::size_t kCapacity = 4;

    [[nodiscard]] PushResult push(QImage image, int frameNumber)
    {
        if (image.isNull()) {
            return {};
        }
        bool dropped = false;
        if (frames_.size() == kCapacity) {
            frames_.pop_front();
            dropped = true;
        }
        frames_.push_back(Frame{std::move(image), frameNumber});
        return {true, dropped, frames_.size()};
    }

    [[nodiscard]] std::optional<Frame> take()
    {
        if (frames_.empty()) {
            return std::nullopt;
        }
        Frame frame = std::move(frames_.front());
        frames_.pop_front();
        return frame;
    }

    void clear() { frames_.clear(); }

    [[nodiscard]] std::size_t depth() const noexcept { return frames_.size(); }
    [[nodiscard]] bool empty() const noexcept { return frames_.empty(); }

    // Capture devices can deliver either true 60/30 or the matching 1000/1001
    // video rate. These helpers describe the two standards; AdaptiveCadence
    // below disciplines the display-only presenter between them. Experimental
    // rates are display-capped at 60 Hz; detector cadence remains independent.
    [[nodiscard]] static constexpr int displayFpsForRequestedFps(int fps) noexcept
    {
        return fps > 0 && fps <= 30 ? 30 : 60;
    }

    [[nodiscard]] static constexpr std::chrono::nanoseconds integerPeriodForRequestedFps(
        int fps) noexcept
    {
        const int displayFps = displayFpsForRequestedFps(fps);
        return std::chrono::nanoseconds{
            (1'000'000'000LL + (displayFps / 2)) / displayFps};
    }

    [[nodiscard]] static constexpr std::chrono::nanoseconds periodForRequestedFps(int fps) noexcept
    {
        const int displayFps = displayFpsForRequestedFps(fps);
        return std::chrono::nanoseconds{1'001'000'000LL / displayFps};
    }

    // Select the display cadence from the sustained capture-delivery rate, never
    // from pixel uniqueness. A static menu can legitimately have zero unique
    // frames while its transport still delivers at 60 Hz. Conversely, some
    // capture devices negotiate a real 30-Hz stream even when 60 was requested;
    // consuming that stream at 60 Hz repeatedly empties/re-primes the queue.
    //
    // Rate telemetry is repeated on every detector payload even though its
    // underlying counter advances only about once per second. The monotonic
    // observation timestamp therefore belongs in this pure policy: repeated
    // copies inside one measurement window cannot satisfy the hysteresis.
    class SourceRateClass final {
    public:
        void reset(int requestedFps) noexcept
        {
            requestedDisplayFps_ = displayFpsForRequestedFps(requestedFps);
            displayFps_ = requestedDisplayFps_;
            pendingDisplayFps_ = displayFps_;
            pendingSamples_ = 0;
            lastObservationMs_ = -1;
        }

        [[nodiscard]] bool observeCaptureFps(int captureFps, std::int64_t nowMs) noexcept
        {
            if (nowMs < 0
                || (lastObservationMs_ >= 0
                    && nowMs - lastObservationMs_ < kObservationIntervalMs)) {
                return false;
            }
            lastObservationMs_ = nowMs;

            int candidate = displayFps_;
            if (requestedDisplayFps_ <= 30) {
                candidate = 30; // never exceed an explicit 30-FPS request
            } else if (captureFps >= kThirtyMinFps && captureFps <= kThirtyMaxFps) {
                candidate = 30;
            } else if (captureFps >= kSixtyMinFps) {
                candidate = 60;
            } else {
                // Zero, a severe collapse, or the indeterminate 37-44 FPS band
                // is a source-health condition, not a new video-rate contract.
                pendingDisplayFps_ = displayFps_;
                pendingSamples_ = 0;
                return false;
            }

            if (candidate == displayFps_) {
                pendingDisplayFps_ = displayFps_;
                pendingSamples_ = 0;
                return false;
            }
            if (candidate != pendingDisplayFps_) {
                pendingDisplayFps_ = candidate;
                pendingSamples_ = 1;
            } else {
                ++pendingSamples_;
            }

            const int requiredSamples = candidate == 30
                ? kDowngradeSamples : kUpgradeSamples;
            if (pendingSamples_ < requiredSamples) {
                return false;
            }
            displayFps_ = candidate;
            pendingDisplayFps_ = candidate;
            pendingSamples_ = 0;
            return true;
        }

        [[nodiscard]] int displayFps() const noexcept { return displayFps_; }

    private:
        static constexpr std::int64_t kObservationIntervalMs = 750;
        static constexpr int kThirtyMinFps = 24;
        static constexpr int kThirtyMaxFps = 36;
        static constexpr int kSixtyMinFps = 45;
        static constexpr int kDowngradeSamples = 3;
        static constexpr int kUpgradeSamples = 2;

        int requestedDisplayFps_ = 60;
        int displayFps_ = 60;
        int pendingDisplayFps_ = 60;
        int pendingSamples_ = 0;
        std::int64_t lastObservationMs_ = -1;
    };

    // Bounded occupancy servo for the presentation clock. A fixed 59.94-Hz
    // clock overfills this four-frame queue once every ~16.7 seconds when the
    // source is true 60 Hz; a fixed 60-Hz clock has the symmetric underflow on
    // a 59.94-Hz source. The integral term learns that small source-rate offset
    // and retains it while the reserve is on target. The proportional term
    // gently rebuilds/drains a frame after ordinary delivery bursts.
    //
    // Corrections are deliberately tiny and bounded (at 60 Hz, <= +/-0.2 ms
    // including the largest reachable reserve error), so this can neither
    // burst-fire frames nor turn a source fault into unbounded display latency.
    // Empty timer wakes do not integrate, preventing wind-up while a producer
    // is stopped. All state is presentation-only and reset per source lifecycle.
    class AdaptiveCadence final {
    public:
        void reset(int requestedFps) noexcept
        {
            const int displayFps = displayFpsForRequestedFps(requestedFps);
            scale_ = 60 / displayFps;
            basePeriodNs_ = integerPeriodForRequestedFps(requestedFps).count();
            integralCorrectionNs_ = 0;
            reserveError_ = 0;
        }

        void observePresentedReserve(std::size_t remainingDepth) noexcept
        {
            const auto boundedDepth = std::min(remainingDepth, kCapacity);
            reserveError_ = static_cast<int>(kRecoveryDepth)
                - static_cast<int>(boundedDepth);
            const std::int64_t integralLimit = kIntegralLimitNsAt60 * scale_;
            integralCorrectionNs_ = std::clamp(
                integralCorrectionNs_
                    + (kIntegralStepNsAt60 * scale_ * reserveError_),
                -integralLimit,
                integralLimit);
        }

        void observeEmptyWake() noexcept
        {
            // Apply bounded proportional recovery without teaching the clock
            // that a stopped/disconnected producer has a lower frame rate.
            reserveError_ = static_cast<int>(kRecoveryDepth);
        }

        [[nodiscard]] std::chrono::nanoseconds period() const noexcept
        {
            const std::int64_t proportional =
                kProportionalStepNsAt60 * scale_ * reserveError_;
            return std::chrono::nanoseconds{
                std::max<std::int64_t>(1,
                    basePeriodNs_ + integralCorrectionNs_ + proportional)};
        }

        [[nodiscard]] std::int64_t integralCorrectionNs() const noexcept
        {
            return integralCorrectionNs_;
        }

    private:
        static constexpr std::int64_t kIntegralStepNsAt60 = 1'000;
        static constexpr std::int64_t kProportionalStepNsAt60 = 50'000;
        static constexpr std::int64_t kIntegralLimitNsAt60 = 100'000;

        int scale_ = 1;
        std::int64_t basePeriodNs_ = 16'666'667;
        std::int64_t integralCorrectionNs_ = 0;
        int reserveError_ = 0;
    };

    struct CadenceSchedule final {
        std::int64_t deadlineNs = 0;
        std::int64_t delayNs = 1;
    };

    // A repeating GUI timer accumulates every late Windows wakeup. At 60 Hz a
    // seemingly harmless 0.5 ms average delay turns 59.94 source frames into a
    // visibly uneven 57-58 FPS handoff. Keep an absolute phase instead: a late
    // callback shortens only the following interval, while a long GUI stall
    // skips deadlines that can no longer be displayed instead of burst-firing
    // several stale frames.
    [[nodiscard]] static constexpr CadenceSchedule scheduleAfterTick(
        std::int64_t previousDeadlineNs,
        std::int64_t nowNs,
        std::int64_t periodNs) noexcept
    {
        const std::int64_t safePeriod = periodNs > 0 ? periodNs : 1;
        std::int64_t nextDeadline = previousDeadlineNs > 0
            ? previousDeadlineNs + safePeriod
            : nowNs + safePeriod;
        if (nextDeadline <= nowNs) {
            const std::int64_t missed = ((nowNs - nextDeadline) / safePeriod) + 1;
            nextDeadline += missed * safePeriod;
        }
        return {nextDeadline, std::max<std::int64_t>(1, nextDeadline - nowNs)};
    }

    // A render-driven presenter must select the render tick nearest its ideal
    // 30/60-Hz deadline. The tolerance is half the measured *render* interval,
    // not half the source period: that distinction preserves even pacing on
    // 120/144-Hz displays while still accepting sub-millisecond clock skew on
    // a 60-Hz display. The caller consumes at most one frame for a true result.
    [[nodiscard]] static constexpr bool renderTickIsDue(
        std::int64_t deadlineNs,
        std::int64_t nowNs,
        std::int64_t renderTickPeriodNs) noexcept
    {
        if (deadlineNs <= 0 || nowNs >= deadlineNs) {
            return true;
        }
        const std::int64_t safeRenderPeriod = std::max<std::int64_t>(
            1, renderTickPeriodNs);
        const std::int64_t toleranceNs = safeRenderPeriod / 2;
        return deadlineNs - nowNs <= toleranceNs;
    }

private:
    std::deque<Frame> frames_;
};

static_assert(PreviewPresentationBuffer::kPrimeDepth
              <= PreviewPresentationBuffer::kCapacity);
static_assert(PreviewPresentationBuffer::kRecoveryDepth
              < PreviewPresentationBuffer::kPrimeDepth);

} // namespace orion
