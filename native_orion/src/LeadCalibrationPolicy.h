#pragma once

#include <algorithm>
#include <cmath>

namespace orion {

// [ORION_LEAD_CALIBRATION] Guided lead calibration -- bisection with reversal-triggered step
// reduction, driven by the game's own TIMING banner.
//
// WHY THIS EXISTS. Everything else in the timing stack now self-tunes: the animation constant is
// learned per jumpshot, shrinks in from three landings, and persists across sessions, so a user
// on a different build converges automatically (confirmed live -- the bot adapted to a different
// jumpshot AND a different release speed without help). The LEAD does not. It is this machine's
// command-to-visible-effect latency, the capture card is ~160 ms of the owner's ~300 ms loop, and
// capture hardware varies enormously between users. So the one number with the largest
// per-machine spread is the only one with no path to being found.
//
// The owner found theirs by walking a slider across many sessions, reading the banner, and having
// the logs analysed in between. A demo user has none of that: without this, their first
// experience is a bot mistiming every shot with no way to fix it.
//
// WHY THE BANNER. It is the ONLY validated outcome signal this project has. Every fill-derived
// alternative has been refuted in turn -- settled_fill moved 0.28 pp for 18 ms of lead,
// peak_fill/f_stop/travel_pp are circular, and a 2026-08-05 check found the batch the owner
// called "tons of earlies" had the HIGHEST median peak fill of three. The user can read the
// banner; the bot cannot. That asymmetry is the whole design: human as oracle.
//
// IT ALSO ABSORBS MORE THAN LATENCY. Shifting when the command fires corrects a shifted TARGET
// just as well as it corrects a shifted delay, so this silently compensates for 2K's own
// "Shot Timing Release Time" setting and for any residual error in the shipped animation
// constant. One calibration, several error sources.
enum class LeadVerdict {
    Early,   // banner said EARLY  -> the release landed BEFORE the tip
    Good,    // banner said EXCELLENT / greened
    Late,    // banner said LATE   -> the release landed AFTER the tip
    Skip,    // banner missed; contributes nothing and must not move anything
};

struct LeadCalibrationState final {
    double leadMs = 0.0;
    double stepMs = 20.0;
    int goodRun = 0;         // consecutive Good verdicts
    int lastDirection = 0;   // -1 = last move decreased lead, +1 = increased, 0 = none yet
    int shots = 0;           // graded shots (Skip does not count)
    bool locked = false;
};

class LeadCalibrationPolicy final {
public:
    // AppConfig bounds. Deliberately NOT widened here: a calibration that can walk outside the
    // range the rest of the engine validates would produce a lead the engine then refuses.
    static constexpr double kMinLeadMs = 150.0;
    static constexpr double kMaxLeadMs = 800.0;
    // Below this the remaining move is smaller than the shot-to-shot spread it is being judged
    // against (predictor rMAD is 10-17 ms), so further steps are fitting noise.
    static constexpr double kLockStepMs = 2.0;
    static constexpr double kMinStepMs = 1.0;
    static constexpr int kLockGoodRun = 3;

    [[nodiscard]] static LeadCalibrationState begin(double currentLeadMs) noexcept
    {
        LeadCalibrationState s;
        s.leadMs = clampLead(currentLeadMs);
        s.stepMs = 20.0;
        return s;
    }

    // THE SIGN, which the spec flags as having been inverted in UI copy before and which is
    // therefore pinned by test:
    //
    //   The command is submitted at (predicted tip - lead) and becomes visible `lead` ms later.
    //   A LARGER lead therefore submits EARLIER. So a banner reading EARLY means the lead is too
    //   BIG and must come DOWN. LATE means it is too small and must go UP.
    //
    // Getting this backwards does not merely fail to converge -- it diverges, driving the user
    // away from their correct lead while asking them to keep shooting.
    [[nodiscard]] static LeadCalibrationState apply(const LeadCalibrationState& prev,
                                                    LeadVerdict verdict) noexcept
    {
        LeadCalibrationState s = prev;
        if (s.locked || verdict == LeadVerdict::Skip) {
            // A missed banner is not evidence. It must not move the lead, reset the good run, or
            // count toward convergence -- otherwise a user who looks away twice locks in a wrong
            // value while believing they measured it.
            return s;
        }

        ++s.shots;

        if (verdict == LeadVerdict::Good) {
            ++s.goodRun;
            if (s.goodRun >= kLockGoodRun) {
                s.locked = true;
            }
            return s;
        }

        // Any graded miss breaks the run: three CONSECUTIVE goods is the lock condition, so an
        // early-good-good-late sequence must not be one step from locking.
        s.goodRun = 0;

        const int direction = (verdict == LeadVerdict::Early) ? -1 : +1;
        if (s.lastDirection != 0 && direction != s.lastDirection) {
            // Reversal: we have bracketed the answer, so halve the step. This is what makes it
            // converge in 10-15 shots rather than oscillating at a fixed amplitude forever.
            s.stepMs = std::max(s.stepMs * 0.5, kMinStepMs);
        }
        s.lastDirection = direction;
        s.leadMs = clampLead(s.leadMs + direction * s.stepMs);

        if (s.stepMs < kLockStepMs) {
            // The step is now finer than the noise it is being judged against. Stop.
            s.locked = true;
        }
        return s;
    }

    [[nodiscard]] static double clampLead(double leadMs) noexcept
    {
        if (!std::isfinite(leadMs)) {
            return kMinLeadMs;
        }
        return std::clamp(leadMs, kMinLeadMs, kMaxLeadMs);
    }
};

} // namespace orion
