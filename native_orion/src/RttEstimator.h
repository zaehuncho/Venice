#pragma once

#include <algorithm>
#include <cmath>

namespace orion {

// Robust one-way-offset estimator from matched out->in RTT samples.
//
// Replaces the old inline EMA (which had no outlier rejection, so a single network spike yanked the
// RTT used in the shot-release lead). Design:
//   - EMA-smoothed RTT for steady state.
//   - MAD-style outlier gate: a sample more than `outlierFactor * max(jitter, floor)` from the
//     current RTT is HELD OUT (a transient spike can't move the estimate)...
//   - ...but a *sustained* deviation (>= outlierAdoptAfter in a row) is adopted as a genuine level
//     shift, so a real route/RTT change is tracked instead of being rejected forever.
//   - RFC3550-style interarrival jitter (mean deviation, /16 gain).
//   - offset = rtt/2 (one-way), syncAdjust = offset + a small jitter margin (capped).
//
// Pure + header-only on purpose: it is unit-tested directly (no network needed) and shared by
// NetworkBridge. All timing is in milliseconds.
struct RttEstimator {
    double rttMs = 0.0;
    double jitterMs = 0.0;
    double offsetMs = 0.0;
    double syncAdjustMs = 0.0;
    int    outlierStreak = 0;

    // Tunables (defaults chosen for a ~10-80ms LAN/Remote-Play RTT with occasional spikes).
    double minSampleMs = 1.0;        // implausible-sample floor (drop below)
    double maxSampleMs = 1000.0;     // implausible-sample ceiling (drop above)
    double alpha = 0.15;             // EMA gain for steady-state RTT
    double outlierFactor = 3.0;      // reject if |sample - rtt| > outlierFactor * max(jitter, floor)
    double jitterFloorMs = 2.0;      // min jitter band so the gate isn't over-tight at startup
    int    outlierAdoptAfter = 4;    // adopt a persistent deviation as a real shift after N in a row
    double syncJitterFactor = 1.2;   // jitter weight in the sync adjustment
    double syncJitterCapMs = 8.0;    // cap on the jitter margin

    // Feed one matched out->in latency sample. Returns true iff it was accepted into the estimate
    // (false = dropped as implausible, or held out as a transient spike).
    bool update(double latencyMs) {
        if (!(latencyMs >= minSampleMs && latencyMs <= maxSampleMs)) {
            return false;  // implausible (torn match / wrong endpoint pairing) -> ignore
        }
        if (rttMs <= 0.0) {            // first sample seeds the estimate
            rttMs = latencyMs;
            jitterMs = 0.0;
            outlierStreak = 0;
            recompute();
            return true;
        }
        const double dev  = std::abs(latencyMs - rttMs);
        const double band = outlierFactor * std::max(jitterMs, jitterFloorMs);
        if (dev > band) {
            // Spike OR a genuine level shift: hold the estimate, but adopt if it persists.
            if (++outlierStreak >= outlierAdoptAfter) {
                rttMs = latencyMs;
                jitterMs = dev * 0.5;   // re-seed jitter around the new level
                outlierStreak = 0;
                recompute();
                return true;
            }
            return false;               // rejected as a transient spike (estimate unchanged)
        }
        outlierStreak = 0;
        jitterMs += (dev - jitterMs) / 16.0;             // RFC3550 interarrival jitter
        rttMs = rttMs * (1.0 - alpha) + latencyMs * alpha;
        recompute();
        return true;
    }

    void reset() { *this = RttEstimator{}; }

private:
    void recompute() {
        offsetMs = rttMs * 0.5;
        syncAdjustMs = offsetMs + std::min(syncJitterCapMs, jitterMs * syncJitterFactor);
    }
};

} // namespace orion
