#pragma once

#include "OrionTypes.h"

#include <QtCore/QRect>
#include <QtCore/QSize>

#include <algorithm>
#include <cmath>

namespace orion {

enum class MeterOverlayBoxAction {
    UpdateFromJoinedBox,
    Clear
};

// Detector freshness and an exact/bounded frame join are both required. The
// ring owns the only permitted two-frame centre bridge; a miss here clears
// instead of painting an old box onto new pixels.
[[nodiscard]] inline MeterOverlayBoxAction meterOverlayBoxAction(
    bool genuineDetectorRecent, bool usableJoinedBox) noexcept
{
    if (!genuineDetectorRecent) {
        return MeterOverlayBoxAction::Clear;
    }
    return usableJoinedBox ? MeterOverlayBoxAction::UpdateFromJoinedBox
                           : MeterOverlayBoxAction::Clear;
}

// A coasted/memory/stale payload may update diagnostics, but it may not
// refresh or replace the box presented as the bot's live meter lock.
[[nodiscard]] inline bool isGenuineMeterOverlayDetection(
    const DetectionResult& result, double staleFrameMaxMs) noexcept
{
    if (!result.detected || result.staleFrame || result.ghostFrame
        || !std::isfinite(result.frameAgeMs) || result.frameAgeMs < 0.0
        || result.frameAgeMs > staleFrameMaxMs) {
        return false;
    }
    const bool acceptedRawReason = result.rejectionReason.isEmpty()
        || result.rejectionReason == QLatin1String("green_not_found");
    const QString stage = result.stage.trimmed().toLower();
    const bool coastedStage = stage == QLatin1String("coast")
        || stage == QLatin1String("no_meter")
        || stage == QLatin1String("stale");
    return acceptedRawReason && !coastedStage;
}

// These reader outcomes deliberately reject the fill value while preserving a
// same-shot, colour/geometry-backed meter position. They are presentation-only:
// none of them may become timing, fill, ETA, or release authority.
[[nodiscard]] inline bool isPositionTrustedMeterOverlayDetection(
    const DetectionResult& result, double staleFrameMaxMs) noexcept
{
    if (!result.detected || result.staleFrame || result.ghostFrame
        || result.width <= 0 || result.height <= 0
        || !std::isfinite(result.frameAgeMs) || result.frameAgeMs < 0.0
        || result.frameAgeMs > staleFrameMaxMs) {
        return false;
    }
    const QString reason = result.rejectionReason.trimmed().toLower();
    if (reason != QLatin1String("fill_gated")
        && reason != QLatin1String("strip_truncated")
        && reason != QLatin1String("held_reseat")
        && reason != QLatin1String("occl_relock")) {
        return false;
    }
    const QString stage = result.stage.trimmed().toLower();
    return stage != QLatin1String("coast")
        && stage != QLatin1String("no_meter")
        && stage != QLatin1String("stale");
}

enum class MeterOverlayContinuityObservation {
    None,
    GenuineStructural,
    TrustedPosition
};

// A display-only lease for short reader-authorized occlusions/reseats. The
// lease can start or renew only from a genuine structural lock in the exact
// active physical-shot epoch. Position-trusted frames may move the box inside
// that lease, but never extend its deadline. This bounds even a continuously
// repeated held payload and leaves AutomationEngine's raw timing gate untouched.
class MeterOverlayContinuityLease final {
public:
    static constexpr qint64 kMaxContinuityMs = 560;

    void reset() noexcept
    {
        shotEpoch_ = 0;
        lastGenuineStructuralMs_ = 0;
    }

    [[nodiscard]] bool active() const noexcept { return shotEpoch_ != 0; }
    [[nodiscard]] quint64 shotEpoch() const noexcept { return shotEpoch_; }

    [[nodiscard]] MeterOverlayContinuityObservation observe(
        const DetectionResult& result, double staleFrameMaxMs, qint64 nowMs,
        bool shotActive, quint64 activeShotEpoch, bool sourceHealthy,
        bool uniqueSourceFrame) noexcept
    {
        if (!sourceHealthy || !shotActive || activeShotEpoch == 0) {
            reset();
            return MeterOverlayContinuityObservation::None;
        }
        if (active() && (shotEpoch_ != activeShotEpoch
                         || nowMs < lastGenuineStructuralMs_
                         || nowMs - lastGenuineStructuralMs_ > kMaxContinuityMs)) {
            reset();
        }
        if (!uniqueSourceFrame) {
            return MeterOverlayContinuityObservation::None;
        }

        if (isGenuineMeterOverlayDetection(result, staleFrameMaxMs)
            && result.gameplayStructureVerified
            && result.gameplayStructureEpoch == activeShotEpoch) {
            shotEpoch_ = activeShotEpoch;
            lastGenuineStructuralMs_ = nowMs;
            return MeterOverlayContinuityObservation::GenuineStructural;
        }
        if (shotEpoch_ == activeShotEpoch
            && isPositionTrustedMeterOverlayDetection(result, staleFrameMaxMs)) {
            return MeterOverlayContinuityObservation::TrustedPosition;
        }
        return MeterOverlayContinuityObservation::None;
    }

    [[nodiscard]] bool maintain(qint64 nowMs, bool shotActive,
                                quint64 activeShotEpoch,
                                bool sourceHealthy) noexcept
    {
        const bool valid = sourceHealthy && shotActive && activeShotEpoch != 0
            && shotEpoch_ == activeShotEpoch && lastGenuineStructuralMs_ > 0
            && nowMs >= lastGenuineStructuralMs_
            && nowMs - lastGenuineStructuralMs_ <= kMaxContinuityMs;
        if (!valid) {
            reset();
        }
        return valid;
    }

private:
    quint64 shotEpoch_ = 0;
    qint64 lastGenuineStructuralMs_ = 0;
};

// Use the video's aspect-fit transform. One uniform scale keeps the bbox's
// geometry intact when the preview is letterboxed or pillarboxed.
[[nodiscard]] inline QRect mapCaptureBoxAspectFit(
    const QRect& captureBox, const QSize& captureSize, const QSize& previewSize) noexcept
{
    if (!captureBox.isValid() || !captureSize.isValid() || !previewSize.isValid()) {
        return {};
    }
    const double scale = std::min(
        static_cast<double>(previewSize.width()) / captureSize.width(),
        static_cast<double>(previewSize.height()) / captureSize.height());
    const double offsetX = (previewSize.width() - captureSize.width() * scale) * 0.5;
    const double offsetY = (previewSize.height() - captureSize.height() * scale) * 0.5;
    const int sourceLeft = std::clamp(captureBox.x(), 0, captureSize.width());
    const int sourceTop = std::clamp(captureBox.y(), 0, captureSize.height());
    const int sourceRight = std::clamp(
        captureBox.x() + captureBox.width(), 0, captureSize.width());
    const int sourceBottom = std::clamp(
        captureBox.y() + captureBox.height(), 0, captureSize.height());
    if (sourceRight <= sourceLeft || sourceBottom <= sourceTop) {
        return {};
    }

    // Transform the two edges, not x/y and width/height independently. Independent
    // rounding can shrink a small half-court box by one preview pixel (for example,
    // 3 source pixels at 0.8x rounded to width 2), visibly clipping its cap/apex.
    // Outward floor/ceil is the minimal aspect-fit rectangle containing every mapped
    // source pixel; both axes still use exactly the same uniform scale.
    const int left = std::max(0, static_cast<int>(std::floor(
        offsetX + sourceLeft * scale)));
    const int top = std::max(0, static_cast<int>(std::floor(
        offsetY + sourceTop * scale)));
    const int right = std::min(previewSize.width(), static_cast<int>(std::ceil(
        offsetX + sourceRight * scale)));
    const int bottom = std::min(previewSize.height(), static_cast<int>(std::ceil(
        offsetY + sourceBottom * scale)));
    return right > left && bottom > top
        ? QRect(left, top, right - left, bottom - top) : QRect{};
}

// Presentation boundary for an already frame-joined/mapped QRect. Observed
// geometry is returned exactly: no guard, size latch, deadband, smoothing, or
// second prediction stage. MeterBoxRing owns the bounded missing-frame bridge.
class MeterOverlayPresentationTracker final {
public:
    void reset() noexcept { valid_ = false; }

    [[nodiscard]] bool valid() const noexcept { return valid_; }

    [[nodiscard]] QRect update(
        const QRect& rawBox, quint64 /*shotToken*/, const QSize& previewSize,
        int /*frameNumber*/) noexcept
    {
        if (!rawBox.isValid() || rawBox.width() <= 0 || rawBox.height() <= 0
            || !previewSize.isValid()) {
            reset();
            return {};
        }

        const QRect exact = rawBox.intersected(QRect(QPoint(0, 0), previewSize));
        if (!exact.isValid()) {
            reset();
            return {};
        }
        valid_ = true;
        return exact;
    }

private:
    bool valid_ = false;
};

// Resolve a detector result which arrived after its immutable preview snapshot
// was published but before that snapshot reached Image.Ready. A fresh local
// helper applies the same exact aspect-fit mapping as normal publication without
// rewinding tracker state. It is presentation-only and cannot affect timing.
[[nodiscard]] inline QRect resolveLateMeterOverlayBox(
    const QRect& captureBox,
    const QSize& captureSize,
    const QSize& previewSize,
    quint64 /*shotToken*/,
    int /*sourceFrameNumber*/) noexcept
{
    return mapCaptureBoxAspectFit(captureBox, captureSize, previewSize);
}

} // namespace orion
