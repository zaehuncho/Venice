#pragma once

#include "OrionTypes.h"

#include <QtCore/QRect>
#include <QtCore/QSize>

#include <algorithm>
#include <cmath>

namespace orion {

// Presentation-only continuity budget. The supplied live trace measured the
// full-resolution detector at 117.6 ms median, 166.7 ms p90 and 298.4 ms max
// between unique results while the meter itself remained continuously visible.
// Keeping this distinct from the 120 ms measured-value/timing freshness budget
// is load-bearing: a held outline may remain visible, but it can never make a
// stale sample eligible for AutomationEngine, ETA, or release scheduling.
inline constexpr qint64 kMeterOverlayVisualFreshMs = 350;
// Once the automation shot has ended there is no in-animation occlusion left
// to bridge.  Keep only enough presentation slack for the measured detector
// p90 (166.7ms) plus two 60Hz frames of presentation/scheduling slack.  This
// prevents the outline from hanging
// over an empty court for the full in-shot lease after the meter disappears.
inline constexpr qint64 kMeterOverlayIdleVisualFreshMs = 200;

[[nodiscard]] inline bool meterOverlayVisualRecent(
    bool runtimeHealthy, qint64 lastGenuineDetectionMs, qint64 nowMs,
    bool shotActive = true) noexcept
{
    const qint64 freshnessMs = shotActive
        ? kMeterOverlayVisualFreshMs : kMeterOverlayIdleVisualFreshMs;
    return runtimeHealthy && lastGenuineDetectionMs > 0
        && nowMs >= lastGenuineDetectionMs
        && nowMs - lastGenuineDetectionMs < freshnessMs;
}

enum class MeterOverlayBoxAction {
    UpdateFromJoinedBox,
    HoldLastJoinedBox,
    Clear
};

// Detector freshness is the visibility authority.  An exact/bounded frame
// join is the movement authority.  A preview frame can arrive before its
// detector metadata, especially while inference is under load; clearing the
// last correctly joined box for that one presentation frame creates the
// visible lock blink the join was meant to prevent.  Hold the prior joined
// box while the genuine-detector freshness lease is still alive, but never
// move it from unjoined/latest metadata.  Once freshness expires, clear it.
[[nodiscard]] inline MeterOverlayBoxAction meterOverlayBoxAction(
    bool genuineDetectorRecent, bool usableJoinedBox) noexcept
{
    if (!genuineDetectorRecent) {
        return MeterOverlayBoxAction::Clear;
    }
    return usableJoinedBox ? MeterOverlayBoxAction::UpdateFromJoinedBox
                           : MeterOverlayBoxAction::HoldLastJoinedBox;
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

// Presentation-only liveness refinement.  Immediately after the meter leaves,
// the reader can emit one or two geometrically valid empty echoes: detected=1,
// fill=0, no green band and no fill-estimator identity.  They are harmless to
// timing (which has its own strict freshness gate), but renewing the visual
// lease from them is what leaves a stale outline after a shot. A real active
// shot always wins; outside that window even a trailing structure bit must be
// accompanied by actual fill/green/ruler evidence.
[[nodiscard]] inline bool isLiveMeterOverlayVisualEvidence(
    const DetectionResult& result, double staleFrameMaxMs,
    bool shotActive) noexcept
{
    if (!isGenuineMeterOverlayDetection(result, staleFrameMaxMs)) {
        return false;
    }
    if (shotActive) {
        return true;
    }
    const QString estimator = result.fillEstimatorMode.trimmed().toLower();
    const bool hasEstimator = result.fillEstimatorGeneration != 0
        || (!estimator.isEmpty() && estimator != QLatin1String("none"));
    const bool hasGreenBand = std::isfinite(result.greenStartPct)
        && std::isfinite(result.greenEndPct)
        && result.greenStartPct >= 0.0
        && result.greenEndPct >= result.greenStartPct;
    return hasEstimator || hasGreenBand
        || (std::isfinite(result.fillPct) && result.fillPct > 0.0);
}

// A new outline may be born only from meter-specific structure proven in the
// current physical shot.  Once born, later exact-frame observations may follow
// the same visual identity.  This is deliberately presentation-only: it neither
// upgrades a DetectionResult nor grants AutomationEngine ownership/timing
// authority.  The bounded identity gate prevents an idle menu/court candidate
// from teleporting a prior shot's box elsewhere on screen.
[[nodiscard]] inline bool meterOverlaySameVisualIdentity(
    const QRect& prior, const QRect& candidate) noexcept
{
    if (!prior.isValid() || !candidate.isValid()
        || prior.width() <= 0 || prior.height() <= 0
        || candidate.width() <= 0 || candidate.height() <= 0) {
        return false;
    }
    const double displacement = std::hypot(
        static_cast<double>(candidate.center().x() - prior.center().x()),
        static_cast<double>(candidate.center().y() - prior.center().y()));
    const double identityCutPx = std::max(
        48.0, 0.75 * std::max(prior.height(), candidate.height()));
    const double widthRatio = std::abs(candidate.width() - prior.width())
        / static_cast<double>(std::max(prior.width(), candidate.width()));
    const double heightRatio = std::abs(candidate.height() - prior.height())
        / static_cast<double>(std::max(prior.height(), candidate.height()));
    return displacement <= identityCutPx
        && widthRatio <= 0.35 && heightRatio <= 0.35;
}

[[nodiscard]] inline bool meterOverlayMayAcquireOrContinue(
    const DetectionResult& result, quint64 physicalShotEpoch,
    bool priorVisualRecent, const QRect& priorCaptureBox) noexcept
{
    const bool exactCurrentShotProof = result.gameplayStructureVerified
        && physicalShotEpoch != 0
        && result.gameplayStructureEpoch == physicalShotEpoch;
    const QRect candidate(result.x, result.y, result.width, result.height);
    return exactCurrentShotProof
        || (priorVisualRecent
            && meterOverlaySameVisualIdentity(priorCaptureBox, candidate));
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

// Presentation boundary for an already frame-joined/mapped QRect. Position is
// copied from the exact same-frame observation: the reader already stabilizes
// the served geometry, so a second position low-pass only makes the outline trail
// the meter. One-pixel extent chatter is held with a causal deadband; a real size
// change is therefore at most one pixel away and never waits an extra frame.
// New shots, large identity/shape changes, and frame discontinuities snap to the
// raw box. The raw DetectionResult remains the sole timing input; this class
// cannot feed the detector, fill estimator, scheduler, ETA, or release decision.
class MeterOverlayPresentationTracker final {
public:
    void reset() noexcept
    {
        valid_ = false;
        shotToken_ = 0;
        lastFrameNumber_ = -1;
        centerX_ = 0.0;
        centerY_ = 0.0;
        width_ = 0.0;
        height_ = 0.0;
    }

    [[nodiscard]] bool valid() const noexcept { return valid_; }

    [[nodiscard]] QRect update(
        const QRect& rawBox, quint64 shotToken, const QSize& previewSize,
        int frameNumber) noexcept
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
        const double rawCenterX = exact.x() + exact.width() * 0.5;
        const double rawCenterY = exact.y() + exact.height() * 0.5;
        const bool frameDiscontinuity = lastFrameNumber_ >= 0
            && (frameNumber <= lastFrameNumber_
                || frameNumber - lastFrameNumber_ > kMaxContinuousFrameGap);

        bool snap = !valid_ || shotToken_ != shotToken || frameDiscontinuity;
        if (!snap) {
            const double dx = rawCenterX - centerX_;
            const double dy = rawCenterY - centerY_;
            const double displacement = std::hypot(dx, dy);
            const double identityCutPx = std::max(
                kMinIdentityCutPx, kIdentityHeightFactor * std::max(height_,
                    static_cast<double>(exact.height())));
            const double widthRatio = std::abs(exact.width() - width_)
                / std::max(1.0, std::max(width_, static_cast<double>(exact.width())));
            const double heightRatio = std::abs(exact.height() - height_)
                / std::max(1.0, std::max(height_, static_cast<double>(exact.height())));
            snap = displacement > identityCutPx
                || widthRatio > kShapeIdentityRatio
                || heightRatio > kShapeIdentityRatio;
        }

        if (snap) {
            width_ = exact.width();
            height_ = exact.height();
        } else {
            // Suppress only the +/-1 px quantization toggle. Gradual scale
            // changes catch up as soon as they differ by two pixels, so the
            // stabilized extent remains within one pixel of this frame's box.
            if (std::abs(exact.width() - width_) > kSizeDeadbandPx) {
                width_ = exact.width();
            }
            if (std::abs(exact.height() - height_) > kSizeDeadbandPx) {
                height_ = exact.height();
            }
        }

        // The frame-id join already supplies either this frame's measured box
        // or an explicitly bounded prior-frame bridge. A second position filter
        // delays real starts/stops and can overshoot reversals after velocity
        // acquisition. Late metadata uses the exact same-frame map as well;
        // receipt order must not choose whether an extra lag is introduced.
        centerX_ = rawCenterX;
        centerY_ = rawCenterY;
        valid_ = true;
        shotToken_ = shotToken;
        lastFrameNumber_ = frameNumber;
        const int drawWidth = std::max(1, static_cast<int>(std::lround(width_)));
        const int drawHeight = std::max(1, static_cast<int>(std::lround(height_)));
        const int drawLeft = static_cast<int>(std::lround(centerX_ - drawWidth * 0.5));
        const int drawTop = static_cast<int>(std::lround(centerY_ - drawHeight * 0.5));
        const QRect drawn(drawLeft, drawTop, drawWidth, drawHeight);
        return drawn.intersected(QRect(QPoint(0, 0), previewSize));
    }

private:
    static constexpr int kMaxContinuousFrameGap = 3;
    static constexpr double kSizeDeadbandPx = 1.0;
    static constexpr double kMinIdentityCutPx = 48.0;
    static constexpr double kIdentityHeightFactor = 0.75;
    static constexpr double kShapeIdentityRatio = 0.35;

    bool valid_ = false;
    quint64 shotToken_ = 0;
    int lastFrameNumber_ = -1;
    double centerX_ = 0.0;
    double centerY_ = 0.0;
    double width_ = 0.0;
    double height_ = 0.0;
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
