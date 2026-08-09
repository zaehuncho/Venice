#pragma once

namespace orion {

// Session-scoped policy for surfacing an explicit timing-setup requirement.
//
// A cold estimator must never turn an ordinary gameplay press into a controlled
// latency probe.  This policy therefore claims only a UI/log prompt; it grants
// no controller or release authority.  The user must explicitly start the
// existing two-stage calibration workflow.  A recovered route, a lost measured
// lead, or an involuntarily stopped setup may surface one fresh prompt, while a
// user cancellation remains authoritative for the current Running session.
class LatencyCalibrationSetupPolicy final {
public:
    void onSessionRunning(bool running) noexcept
    {
        if (running && !sessionRunning_) {
            sessionRunning_ = true;
            promptClaimed_ = false;
            suppressed_ = false;
            routeReady_ = false;
            leadReady_ = false;
            calibrationActive_ = false;
        } else if (!running) {
            sessionRunning_ = false;
            routeReady_ = false;
            leadReady_ = false;
            calibrationActive_ = false;
        }
    }

    [[nodiscard]] bool claimPrompt(bool routeReady, bool leadReady,
                                   bool calibrationActive) noexcept
    {
        if (routeReady && !routeReady_) {
            promptClaimed_ = false;
        }
        if (!leadReady && leadReady_) {
            promptClaimed_ = false;
        }
        if (!calibrationActive && calibrationActive_) {
            promptClaimed_ = false;
        }
        routeReady_ = routeReady;
        leadReady_ = leadReady;
        calibrationActive_ = calibrationActive;

        if (!sessionRunning_ || !routeReady || leadReady || calibrationActive
            || promptClaimed_ || suppressed_) {
            return false;
        }
        promptClaimed_ = true;
        return true;
    }

    void suppressForCurrentSession() noexcept
    {
        if (sessionRunning_) {
            suppressed_ = true;
        }
    }

    [[nodiscard]] bool suppressed() const noexcept { return suppressed_; }
    [[nodiscard]] bool promptClaimed() const noexcept { return promptClaimed_; }

private:
    bool sessionRunning_ = false;
    bool promptClaimed_ = false;
    bool suppressed_ = false;
    bool routeReady_ = false;
    bool leadReady_ = false;
    bool calibrationActive_ = false;
};

} // namespace orion
