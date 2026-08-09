#pragma once

#include <QtCore/QRect>

#include <algorithm>
#include <array>
#include <cmath>

namespace orion {

// FRAME-ID JOIN ring buffer.
//
// The autogreen sidecar emits TWO streams that both stamp each frame with the SAME decoder frame
// number (orch._last_decoded_frame_number): the preview `frame` JPEGs (decoded async, delivered
// LATER) and the per-frame detection `telemetry` (bbox applied synchronously). Painting the LATEST
// bbox onto a just-decoded (older) preview image made the on-screen lock box drift/flicker/disappear.
//
// This ring records each detection's capture-px bbox keyed by its decoder frame number; at paint time
// the overlay looks up the box detected on the EXACT preview frame being drawn (or the nearest
// slightly-earlier detection), so the box composites on its own frame. A small drop-oldest ring is
// plenty: detections arrive at 30-60/s and the preview is at most a few frames behind the capture.
class MeterBoxRing final {
public:
    static constexpr int kSize = 16;
    // A detector result can arrive just after its matching preview. Bridge at
    // most two source frames; never turn freshness into a long stale-box lease.
    static constexpr int kJoinWindow = 2;

    // Record the capture-px bbox for a decoder frame. Frames without a valid id (<0) or an empty box
    // are ignored (older sidecar / GDI tier) — the caller falls back to latest-box behaviour.
    void record(int frameNumber, const QRect& captureBox) {
        if (frameNumber < 0 || captureBox.isNull()) {
            return;
        }
        for (auto& sample : ring_) {
            if (sample.frameNumber == frameNumber) {
                sample.box = captureBox;
                return;
            }
        }
        ring_[head_] = Sample{frameNumber, captureBox};
        head_ = (head_ + 1) % kSize;
    }

    // Decoder frame ids restart with each sidecar/session. Clear the old id
    // namespace so a low-number frame cannot join to a prior session's box.
    void clear() noexcept {
        ring_ = {};
        head_ = 0;
    }

    [[nodiscard]] bool empty() const noexcept {
        for (const auto& sample : ring_) {
            if (sample.frameNumber >= 0) {
                return false;
            }
        }
        return true;
    }

    // Per-frame cap for the display-only centre bridge. The raw dimensions from
    // the newest genuine detection remain immutable.
    static constexpr int kMaxBridgePxPerFrame = 24;

    // Exact match when available; otherwise use the closest earlier detection
    // within the strict bridge window. Two earlier samples may advance only the
    // centre. Width and height remain those of the newest genuine detection.
    // NOTE: cosmetic only — the actual fire times on telemetry, never on this drawn box.
    [[nodiscard]] bool lookup(
        int frameNumber, QRect& outCaptureBox,
        int* matchedDetectionFrameNumber = nullptr) const {
        if (frameNumber < 0) {
            return false;
        }
        int best1Frame = -1;          // nearest earlier
        QRect best1Box;
        int best2Frame = -1;          // second-nearest earlier (for velocity)
        QRect best2Box;
        for (const auto& s : ring_) {
            if (s.frameNumber < 0) {
                continue;   // empty slot
            }
            if (s.frameNumber == frameNumber) {
                if (matchedDetectionFrameNumber) {
                    *matchedDetectionFrameNumber = s.frameNumber;
                }
                outCaptureBox = s.box;   // exact join — no extrapolation needed
                return true;
            }
            if (s.frameNumber < frameNumber && s.frameNumber > best1Frame) {
                best2Frame = best1Frame;   // demote previous best to second
                best2Box = best1Box;
                best1Frame = s.frameNumber;
                best1Box = s.box;
            } else if (s.frameNumber < best1Frame && s.frameNumber > best2Frame) {
                best2Frame = s.frameNumber;
                best2Box = s.box;
            }
        }
        if (best1Frame < 0 || (frameNumber - best1Frame) > kJoinWindow) {
            return false;
        }
        QRect box = best1Box;
        // Predict only the centre. Size always comes from the nearest genuine
        // sample, even if the preceding sample used a different detector extent.
        if (best2Frame >= 0 && best1Frame > best2Frame) {
            const int dfMeasure = best1Frame - best2Frame;
            const int dfExtrap = frameNumber - best1Frame;
            const double firstCentreX = best1Box.x() + 0.5 * best1Box.width();
            const double firstCentreY = best1Box.y() + 0.5 * best1Box.height();
            const double secondCentreX = best2Box.x() + 0.5 * best2Box.width();
            const double secondCentreY = best2Box.y() + 0.5 * best2Box.height();
            const double vx = (firstCentreX - secondCentreX) / dfMeasure;
            const double vy = (firstCentreY - secondCentreY) / dfMeasure;
            const int maxBridgePx = kMaxBridgePxPerFrame * dfExtrap;
            const int dx = std::clamp(
                static_cast<int>(std::lround(vx * dfExtrap)),
                -maxBridgePx, maxBridgePx);
            const int dy = std::clamp(
                static_cast<int>(std::lround(vy * dfExtrap)),
                -maxBridgePx, maxBridgePx);
            box.translate(dx, dy);
        }
        outCaptureBox = box;
        if (matchedDetectionFrameNumber) {
            *matchedDetectionFrameNumber = best1Frame;
        }
        return true;
    }

private:
    struct Sample {
        int frameNumber = -1;   // decoder frame seq; -1 = empty slot
        QRect box;              // capture-frame px
    };
    std::array<Sample, kSize> ring_{};
    int head_ = 0;   // next write index (wraps)
};

} // namespace orion
