#pragma once

// [ORION_TIP_FRAME_NATIVE 2026-09-15] THE GAME'S FRAME GRID, RECOVERED FROM THE FILL STAIRCASE.
//
// WHAT THIS IS. The console renders and grades at 60 Hz, and the shot meter's fill advances
// exactly ONE STEP PER GAME FRAME. The capture runs at 60 Hz too, but on its own clock: mostly a
// 1:1 relay of console frames, with an occasional duplicate or skipped frame when the two beat
// against each other. So every capture sample whose fill VALUE differs from the previous one is
// the first sighting of a new GAME FRAME, and the timestamps of those step edges are the console's
// frame boundaries plus one constant transport delay.
//
// This estimator fits a single 60 Hz grid to those step-edge timestamps (a least-squares
// phase-locked loop: integer frame indices from the edge spacing, one shared offset) and hands
// back the grid, its residual sd, and the boundaries/centres of individual frames.
//
// WHAT THE CONSTANT TRANSPORT DELAY MEANS, AND WHY IT IS NOT A DEFECT. The grid this recovers is
// the console's frame grid shifted by the video path's delay -- the same delay that is already
// inside every capture-clock quantity the engine owns, and the same one the owner's Shot Lead was
// calibrated against. It is common to the anchor, to the tip, and to the frame centre, so it
// cancels out of every DIFFERENCE and is absorbed by the lead in every ABSOLUTE target. That is
// precisely why a frame-centred fire target may shift the mean release by up to half a frame
// (8.3 ms) without re-aiming anything: the banner shows the shift once and the lead absorbs it.
//
// WHAT IT IS FOR (two consumers, in order of how much they are worth):
//   1. PHASE-ALIGNED FIRING. The game samples the pad ONCE PER FRAME, so a release quantises onto
//      this grid. A target at a random phase inside the tip's frame is one jitter-width away from
//      falling into the neighbouring frame; a target at the frame's CENTRE has the maximum
//      possible margin (+-8.3 ms) before that happens. Measured on the synthetic model
//      (scratchpad proto2.py, 200k trials per cell): P(release lands in the intended frame) at
//      predictor sigma 11.7 / 8.0 / 5.0 ms is 49.0 / 62.3 / 76.0 % for a uniformly-phased target
//      and 52.2 / 70.2 / 90.6 % for a frame-centred one. That is the whole point of this header.
//   2. FRAME-NATIVE ANCHOR DATING. The date of a rung crossing becomes "the boundary of the game
//      frame the fill crossed in, less the intra-frame overshoot the curve model prices", instead
//      of a straight-line interpolation across one straddling pair of capture samples.
//
// WHAT THIS HEADER DOES *NOT* CLAIM -- MEASURED, 2026-09-15. The premise this work started from
// was that the straddling-pair interpolation in notePhaseAnchorSample is "up to +-8 ms wrong
// depending on capture phase". On a modelled 60 Hz staircase driven by the shipped curve table
// that is REFUTED: shifting the capture grid's phase moves BOTH the interpolated date and the
// frame-native date by exactly the same constant, and against the true continuous crossing the
// interpolation is unbiased with sd 0.08 ms over a full sweep of the animation's own sub-frame
// phase (0.17 ms even across a skipped capture frame at the crossing). The interpolation is a
// GOOD sub-frame estimator; the staircase's own step positions carry the phase information it
// needs. What frame-native dating still earns is narrower and honest: it averages the capture
// timestamp over every step edge of the shot instead of trusting one pair (sd 2.23 vs 2.77 ms at
// 3 ms of per-sample timestamp jitter), and it is the measurement that makes (1) possible at all.
// The sigma the predictor reports is therefore NOT narrowed by default -- see
// RemapConfig::tipFrameNativeSigmaCreditMs.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>

namespace orion {
namespace game_frame_phase {

inline constexpr double kDefaultPeriodMs = 1000.0 / 60.0;

struct Estimate final {
    int edges = 0;
    // Boundaries of the recovered grid sit at phaseMs + k * periodMs on the CAPTURE clock.
    // phaseMs is an absolute offset, not a residue: callers use the helpers, never the raw value.
    double phaseMs = 0.0;
    double sdMs = -1.0;
    double periodMs = kDefaultPeriodMs;
    int skips = 0;              // capture frames the fill proved were never delivered
    bool locked = false;

    [[nodiscard]] bool valid() const noexcept
    {
        return edges > 0 && std::isfinite(phaseMs) && std::isfinite(periodMs) && periodMs > 0.0;
    }
    // The phase residue inside one frame, which is what a log line wants to show.
    [[nodiscard]] double phaseResidueMs() const noexcept
    {
        if (!valid()) {
            return -1.0;
        }
        double r = std::fmod(phaseMs, periodMs);
        if (r < 0.0) {
            r += periodMs;
        }
        return r;
    }
};

// Pure, allocation-free, and deliberately ignorant of every engine concept: it sees fills and
// timestamps and nothing else. Copy-assignable so it can live inside ShotContext and be reset by
// the same `shot_ = ShotContext{}` that resets every other per-shot clock.
//
// CONSTANT SIZE, BY RUNNING SUMS -- and that is a requirement, not a flourish. ShotContext is
// COPIED BY VALUE on the precise-fire path (OrionAppController takes automation_.context()
// snapshots while the worker is spinning toward a submit), so an estimator that kept its edges in
// an array would put hundreds of bytes of memcpy inside the one code path in this codebase where
// microseconds are the product. A least-squares grid of KNOWN period needs only n and two moments
// of the residuals, so that is all this keeps: eleven scalars, no array, no allocation, and the
// same answer to the last bit.
//
// The moments are accumulated RELATIVE TO THE FIRST RESIDUAL, never as raw engine-clock values.
// Engine timestamps are ~1e5 ms and the variance being measured is ~1 ms^2, so the textbook
// sum-of-squares-minus-square-of-sum would cancel away every significant digit of the answer;
// differencing against the first residual keeps every accumulated quantity within a few ms of
// zero, where double has room to spare.
class Estimator final {
public:
    void configure(double periodMs, int minEdges, double maxSdMs) noexcept
    {
        const double resolvedMs = std::isfinite(periodMs) && periodMs > 1.0 && periodMs < 100.0
            ? periodMs : kDefaultPeriodMs;
        // A CHANGED PERIOD INVALIDATES EVERY ACCUMULATED MOMENT. The running sums are residuals
        // measured against this exact period (t_i - idx_i * P), so keeping them across a change
        // would fit the new grid to arithmetic done on the old one -- a silently wrong phase
        // rather than an honest refusal. Callers configure with the same console period on every
        // sample, so in practice this never fires; it exists so that it cannot go wrong if the
        // console description is ever changed mid-shot.
        if (count_ > 0 && std::abs(resolvedMs - periodMs_) > 1e-9) {
            reset();
        }
        periodMs_ = resolvedMs;
        minEdges_ = std::max(2, minEdges);
        maxSdMs_ = std::isfinite(maxSdMs) && maxSdMs > 0.0 ? maxSdMs : 3.0;
    }

    void reset() noexcept
    {
        count_ = 0;
        skips_ = 0;
        havePrev_ = false;
        prevFillPct_ = -1.0;
        prevEdgeMs_ = -1.0;
        lastSampleMs_ = -1.0;
        prevIndex_ = 0;
        stepCount_ = 0;
        stepSumPct_ = 0.0;
        firstResidualMs_ = 0.0;
        sumDeltaMs_ = 0.0;
        sumDeltaSqMs2_ = 0.0;
    }

    // One accepted (fill, capture-clock) sample. Returns true when it was a STEP EDGE, i.e. the
    // first capture sample carrying a new game frame's fill. A repeated fill (a duplicated
    // capture frame) is not an edge and contributes nothing -- which is the correct reading:
    // no new game frame was delivered.
    bool note(double fillPct, double captureMs) noexcept
    {
        if (!std::isfinite(fillPct) || !std::isfinite(captureMs) || fillPct < 0.0) {
            return false;
        }
        // Reject an old/duplicate source timestamp BEFORE touching the fill
        // baseline or interpreting a drop as a new episode. Even a newer flat
        // observation advances this watermark; the last STEP is not the last
        // accepted sample. Otherwise a delayed low fill can erase a valid grid.
        if (havePrev_ && captureMs <= lastSampleMs_) {
            return false;
        }
        lastSampleMs_ = captureMs;
        if (!havePrev_) {
            havePrev_ = true;
            prevFillPct_ = fillPct;
            pushEdge(captureMs, 0);
            return true;
        }
        const double deltaPct = fillPct - prevFillPct_;
        // A MATERIAL FALL IS A NEW EPISODE, not a step. The staircase we were fitting has been
        // replaced -- a carryover meter preempted by a fresh one, a detector re-lock onto a
        // different box, a spent meter deflating -- and the edges either side of that break do
        // not belong to one continuous 60 Hz fit. Carrying them across would hand the new
        // animation the old one's grid, which is the single worst thing a phase estimator can do.
        // Same reading, and the same fail-closed direction, as the first-sight anchor's own drop
        // retraction; the threshold is deliberately well above the reader's +-0.5 pp noise and
        // above one frame's fill increment, so an ordinary wobble is not an episode break.
        if (deltaPct < -kEpisodeBreakDropPct) {
            reset();
            havePrev_ = true;
            lastSampleMs_ = captureMs;
            prevFillPct_ = fillPct;
            pushEdge(captureMs, 0);
            return true;
        }
        // Only a RISE marks a new frame. A flat or falling fill is a duplicated capture frame, a
        // plateau, or a detector wobble; none of them is evidence that the console advanced.
        if (!(deltaPct > kMinStepPct)) {
            prevFillPct_ = fillPct;
            return false;
        }
        if (!(captureMs > prevEdgeMs_)) {
            prevFillPct_ = fillPct;
            return false;
        }
        // HOW MANY GAME FRAMES DID THIS EDGE ADVANCE? Two independent answers, and the fill's is
        // the one that survives a beat: "a step of ~2x the nominal fill increment means a SKIPPED
        // CAPTURE FRAME, not a phase jump". The timestamp's answer is used only until the fill
        // has established a nominal step, and as the tie-break when the fill says 1.
        int advance = 1;
        const double nominalPct = nominalStepPct();
        if (nominalPct > 0.0) {
            const int fromFill = static_cast<int>(std::lround(deltaPct / nominalPct));
            advance = std::max(1, std::min(kMaxAdvance, fromFill));
        }
        if (advance == 1 && periodMs_ > 0.0) {
            const int fromTime = static_cast<int>(
                std::lround((captureMs - prevEdgeMs_) / periodMs_));
            advance = std::max(1, std::min(kMaxAdvance, fromTime));
        }
        skips_ += advance - 1;
        // The per-frame increment is learned only from CLEAN single-frame steps: feeding a
        // doubled step back into the nominal would ratchet the nominal upward and then read
        // every ordinary step as a skip.
        if (advance == 1) {
            stepSumPct_ += deltaPct;
            ++stepCount_;
        }
        prevFillPct_ = fillPct;
        pushEdge(captureMs, prevIndex_ + advance);
        return true;
    }

    // The fill increment one game frame is currently worth, in percentage points. 0 until at
    // least one clean step has been seen.
    [[nodiscard]] double nominalStepPct() const noexcept
    {
        return stepCount_ > 0 ? stepSumPct_ / static_cast<double>(stepCount_) : 0.0;
    }

    [[nodiscard]] Estimate estimate() const noexcept
    {
        Estimate out;
        out.periodMs = periodMs_;
        out.edges = static_cast<int>(count_);
        out.skips = skips_;
        if (count_ == 0) {
            out.sdMs = -1.0;
            return out;
        }
        // One shared offset, integer indices: a = mean(t_i - idx_i * P). That is the exact
        // least-squares solution for a grid of KNOWN period, and it is what averages the
        // per-sample timestamp jitter down by sqrt(n).
        const double n = static_cast<double>(count_);
        out.phaseMs = firstResidualMs_ + sumDeltaMs_ / n;
        if (count_ < 2) {
            out.sdMs = -1.0;
            out.locked = false;
            return out;
        }
        const double ss = sumDeltaSqMs2_ - sumDeltaMs_ * sumDeltaMs_ / n;
        out.sdMs = std::sqrt(std::max(0.0, ss) / (n - 1.0));
        out.locked = out.edges >= minEdges_ && std::isfinite(out.sdMs) && out.sdMs <= maxSdMs_;
        return out;
    }

    // The grid boundary at or before tMs. NaN when no grid exists.
    [[nodiscard]] double boundaryAtOrBefore(double tMs) const noexcept
    {
        const Estimate e = estimate();
        if (!e.valid() || !std::isfinite(tMs)) {
            return std::numeric_limits<double>::quiet_NaN();
        }
        // +1e-9: a tMs sitting exactly ON a boundary belongs to the frame that boundary starts.
        const double k = std::floor((tMs - e.phaseMs) / e.periodMs + 1e-9);
        return e.phaseMs + k * e.periodMs;
    }

    [[nodiscard]] double nearestBoundary(double tMs) const noexcept
    {
        const Estimate e = estimate();
        if (!e.valid() || !std::isfinite(tMs)) {
            return std::numeric_limits<double>::quiet_NaN();
        }
        return e.phaseMs + std::round((tMs - e.phaseMs) / e.periodMs) * e.periodMs;
    }

    // Centre of the frame CONTAINING tMs -- the maximum-margin instant for a release the console
    // will sample once, at the end of that frame.
    [[nodiscard]] double frameCentreContaining(double tMs) const noexcept
    {
        const double b = boundaryAtOrBefore(tMs);
        if (!std::isfinite(b)) {
            return std::numeric_limits<double>::quiet_NaN();
        }
        return b + 0.5 * periodMs_;
    }

    // How far tMs sits from the NEAREST boundary of its frame (always >= 0, <= period/2). This is
    // the number the near-boundary guard reads: small means "the frame this instant belongs to is
    // a coin flip", and a coin flip must not be resolved by a grid we are not sure of.
    [[nodiscard]] double distanceToNearestBoundaryMs(double tMs) const noexcept
    {
        const double b = boundaryAtOrBefore(tMs);
        if (!std::isfinite(b)) {
            return std::numeric_limits<double>::quiet_NaN();
        }
        const double into = tMs - b;
        return std::min(into, periodMs_ - into);
    }

    [[nodiscard]] double periodMs() const noexcept { return periodMs_; }

private:
    static constexpr double kMinStepPct = 0.05;   // below the reader's own quantisation: not a step
    static constexpr double kEpisodeBreakDropPct = 5.0;   // a fall this far is a NEW staircase
    static constexpr int kMaxAdvance = 4;         // a 5-frame gap is a dropout, not a beat

    void pushEdge(double tMs, long long index) noexcept
    {
        const double residualMs = tMs - static_cast<double>(index) * periodMs_;
        if (count_ == 0) {
            firstResidualMs_ = residualMs;
            sumDeltaMs_ = 0.0;
            sumDeltaSqMs2_ = 0.0;
        } else {
            const double deltaMs = residualMs - firstResidualMs_;
            sumDeltaMs_ += deltaMs;
            sumDeltaSqMs2_ += deltaMs * deltaMs;
        }
        ++count_;
        prevEdgeMs_ = tMs;
        prevIndex_ = index;
    }

    double firstResidualMs_ = 0.0;
    double sumDeltaMs_ = 0.0;
    double sumDeltaSqMs2_ = 0.0;
    std::size_t count_ = 0;
    int skips_ = 0;
    bool havePrev_ = false;
    double prevFillPct_ = -1.0;
    double prevEdgeMs_ = -1.0;
    double lastSampleMs_ = -1.0;
    long long prevIndex_ = 0;
    int stepCount_ = 0;
    double stepSumPct_ = 0.0;
    double periodMs_ = kDefaultPeriodMs;
    int minEdges_ = 3;
    double maxSdMs_ = 3.0;
};

// [ORION_TIP_FRAME_NATIVE 2026-09-17] WHERE DID THE RELEASE ACTUALLY LAND ON THIS GRID?
//
// The counterpart of the aiming rule above, and the one number docs/POLL_PHASE_TRACKER.md §10
// asked for: it could prove the SCHEDULE carried the frame-centre offset and that the FIRE did
// not, but only by rebuilding the grid offline from a different log line. This is the same
// arithmetic, live, at the fire.
//
// THE LEAD IS PART OF THE QUESTION, NOT AN ADJUSTMENT. The grid is on the capture-aligned clock
// and a command becomes VISIBLE to the console `lead` ms after it is issued, so the instant that
// belongs on this grid is issued + lead -- which is exactly the target the arm centred. Placing
// the raw issue instant on the grid instead would measure the lead's own residue mod one frame
// and say nothing whatsoever about centring.
//
// phaseMs is in [0, period); centreDeltaMs is the SIGNED distance from the frame's centre,
// bounded by half a frame, negative when the release landed before the centre. valid == false
// (and the sentinels) whenever the grid never locked or the inputs are not finite -- the caller
// prints the sentinels rather than a fabricated zero, because "we did not measure it" and "it
// landed dead centre" are the two readings this line exists to tell apart.
struct ReleaseGridPhase final {
    double phaseMs = -1.0;
    double centreDeltaMs = -99.0;
    bool valid = false;
};

[[nodiscard]] inline ReleaseGridPhase releasePhaseOnGrid(const Estimate& grid, double releaseMs,
                                                         double leadMs) noexcept
{
    ReleaseGridPhase out;
    if (!grid.locked || !grid.valid() || !std::isfinite(grid.phaseMs) || !std::isfinite(releaseMs)
        || !std::isfinite(leadMs) || grid.periodMs <= 0.0) {
        return out;
    }
    const double visibleMs = releaseMs + leadMs;
    double phaseMs = std::fmod(visibleMs - grid.phaseMs, grid.periodMs);
    if (phaseMs < 0.0) {
        phaseMs += grid.periodMs;
    }
    out.phaseMs = phaseMs;
    out.centreDeltaMs = phaseMs - 0.5 * grid.periodMs;
    out.valid = true;
    return out;
}

}  // namespace game_frame_phase
}  // namespace orion
