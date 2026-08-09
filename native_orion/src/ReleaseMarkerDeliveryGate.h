#pragma once

#include <QtCore/QtGlobal>

#include <cmath>
#include <optional>

namespace orion {

struct DeliveredReleaseMarker {
    int seq = 0;
    double wallMsEpoch = 0.0;
    bool latencyCalibration = false;
    double validationTargetPct = -1.0;
    double validationTolerancePct = -1.0;
    // Immutable ownership identities captured when the release intent was
    // created.  They are telemetry/correlation only: local route acceptance is
    // still the delivery authority and is never promoted to a console ACK.
    quint64 physicalShotEpoch = 0;
    quint64 shotAttempt = 0;
};

// Holds timing intent until the controller route proves that the matching release edge was
// accepted locally.  A failed/mismatched delivery consumes the intent without emitting it, so
// the sidecar latency oracle can never learn from an output that did not reach a live route.
class ReleaseMarkerDeliveryGate final {
public:
    bool stage(int seq, double wallMsEpoch, bool latencyCalibration,
               double validationTargetPct = -1.0,
               double validationTolerancePct = -1.0,
               quint64 physicalShotEpoch = 0,
               quint64 shotAttempt = 0) noexcept
    {
        if (seq <= 0 || !std::isfinite(wallMsEpoch) || wallMsEpoch <= 0.0) {
            reset();
            return false;
        }
        const bool validationPresent = std::isfinite(validationTargetPct)
            && validationTargetPct > 0.0;
        if (validationPresent
            && (!latencyCalibration || validationTargetPct > 95.0
                || !std::isfinite(validationTolerancePct)
                || validationTolerancePct <= 0.0
                || validationTolerancePct > 50.0)) {
            reset();
            return false;
        }
        if (!validationPresent) {
            validationTargetPct = -1.0;
            validationTolerancePct = -1.0;
        }
        // Either both ownership ids are present, or neither is.  A partial
        // identity cannot be correlated safely across the JSON boundary.
        if ((physicalShotEpoch == 0) != (shotAttempt == 0)) {
            reset();
            return false;
        }
        pending_ = DeliveredReleaseMarker{seq, wallMsEpoch, latencyCalibration,
                                          validationTargetPct,
                                          validationTolerancePct,
                                          physicalShotEpoch, shotAttempt};
        return true;
    }

    [[nodiscard]] std::optional<DeliveredReleaseMarker> resolve(
        int deliveredSeq, bool deliverySucceeded, double acceptedWallMsEpoch,
        bool preserveIntentTimestamp) noexcept
    {
        if (!pending_) {
            return std::nullopt;
        }
        const DeliveredReleaseMarker intent = *pending_;
        pending_.reset();
        if (!deliverySucceeded || deliveredSeq <= 0 || deliveredSeq != intent.seq) {
            return std::nullopt;
        }
        DeliveredReleaseMarker delivered = intent;
        if (!preserveIntentTimestamp) {
            if (!std::isfinite(acceptedWallMsEpoch) || acceptedWallMsEpoch <= 0.0) {
                return std::nullopt;
            }
            delivered.wallMsEpoch = acceptedWallMsEpoch;
        }
        return delivered;
    }

    void cancel(int seq) noexcept
    {
        if (pending_ && (seq <= 0 || pending_->seq == seq)) {
            pending_.reset();
        }
    }

    void reset() noexcept { pending_.reset(); }
    [[nodiscard]] bool pending() const noexcept { return pending_.has_value(); }
    [[nodiscard]] int pendingSeq() const noexcept { return pending_ ? pending_->seq : 0; }

private:
    std::optional<DeliveredReleaseMarker> pending_;
};

} // namespace orion
