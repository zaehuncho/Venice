#pragma once

#include <cstdint>

namespace orion {

// [RT-MED-04 / CL3-F4-007 / CL3-F8-009 2026-09-23] The meter-blind warning and the
// DETECTION UNAVAILABLE latch, as one pure state machine the controller feeds and the tests drive.
//
// WHAT WAS WRONG. The controller counted "armed shot epochs with no genuine detection" off
// ShotContext::physicalShotEpoch -- a field only AutomationEngine::beginShot assigns. In METER
// mode a press the detector never sees NEVER begins a shot: it ends as
// `SHOT NOT OWNED: reason=press_unanswered_no_meter`, so the epoch the advisor watched never moved,
// the streak never reached 3, and a patch-day meter (or a wrong in-game style: 80 presses, 0
// releases, 0 warnings on 09-22) produced no explicit unavailable state at all. Two more holes:
//   * any genuine raw detection -- including an idle false lock between shots (~65 per session on
//     09-21) -- reset the streak before the epoch check, so the trip could be starved;
//   * recovery counted ANY new epoch in Holding/GreenWindow as "owned", and a Go-To push enters
//     Holding with release_reason=await_meter and no meter at all.
//
// THE RULES NOW.
//   1. The streak counts COMPLETED controller-origin METER presses that got no meter evidence at
//      all (the engine's press_unanswered_no_meter terminal), one per physical epoch, and only
//      presses held long enough to be a shot (kMinShotHoldMs): taps, pump fakes and menu presses
//      are not a question the meter was asked.
//   2. A raw detection counts only INSIDE an active press epoch. It resets the streak and marks
//      that epoch as sighted (so its own terminal is not counted). Idle sightings do nothing.
//   3. The latch trips at kTripPresses and clears only after kRecoveryOwnedShots distinct
//      VISION-OWNED shots (a genuinely accepted meter this shot) -- never on a bare Holding state.
//
// This is advisory state plus the engine's detection_unavailable gate. It grants no fire
// authority and removes none that the METER-mode green-window priority does not already remove:
// silent-fire protection is the engine's (greenWindowPriority suppresses the blind backstop).
class MeterBlindnessLatch final {
public:
    static constexpr int kTripPresses = 3;
    static constexpr int kRecoveryOwnedShots = 2;
    // A 2K27 jumpshot meter runs ~584 ms and the player holds to the tip; a steal/menu tap or pump
    // fake is well under this. Presses shorter than this never count toward the streak.
    static constexpr double kMinShotHoldMs = 300.0;

    enum class Event {
        None,        // nothing changed that the caller has to announce
        Counted,     // one more unanswered press (streak below the trip)
        Tripped,     // the latch just engaged (DETECTION UNAVAILABLE)
        Recovered,   // the latch just cleared (DETECTION RESTORED)
    };

    void reset() noexcept { *this = MeterBlindnessLatch{}; }

    // Rule 2. activePressEpoch = the physical epoch of the press in flight (owned or still
    // pending), 0 when the player is not pressing anything.
    void observeRawDetection(std::uint64_t activePressEpoch) noexcept
    {
        if (activePressEpoch == 0) {
            return;   // idle sighting: neither proof nor disproof of the shot meter
        }
        sightedEpoch_ = activePressEpoch;
        streak_ = 0;
    }

    // Rule 1.
    Event observeUnansweredPress(std::uint64_t epoch, double holdMs) noexcept
    {
        if (epoch == 0 || epoch == lastCountedEpoch_ || epoch == sightedEpoch_) {
            return Event::None;
        }
        if (!(holdMs >= kMinShotHoldMs)) {
            return Event::None;   // also rejects NaN
        }
        lastCountedEpoch_ = epoch;
        if (streak_ < kTripPresses) {
            ++streak_;
        }
        if (streak_ >= kTripPresses && !unavailable_) {
            unavailable_ = true;
            recoveryOwnedShots_ = 0;
            lastRecoveryEpoch_ = epoch;
            return Event::Tripped;
        }
        return Event::Counted;
    }

    // Rule 3. Call only for a shot vision genuinely owned (a meter was seen and accepted this
    // shot); the caller decides that from the ShotContext.
    Event observeVisionOwnedShot(std::uint64_t epoch) noexcept
    {
        if (epoch == 0) {
            return Event::None;
        }
        streak_ = 0;
        sightedEpoch_ = epoch;
        if (!unavailable_ || epoch == lastRecoveryEpoch_) {
            return Event::None;
        }
        lastRecoveryEpoch_ = epoch;
        if (++recoveryOwnedShots_ >= kRecoveryOwnedShots) {
            unavailable_ = false;
            recoveryOwnedShots_ = 0;
            return Event::Recovered;
        }
        return Event::None;
    }

    [[nodiscard]] bool unavailable() const noexcept { return unavailable_; }
    [[nodiscard]] bool warning() const noexcept
    {
        return unavailable_ || streak_ >= kTripPresses;
    }
    [[nodiscard]] int streak() const noexcept { return streak_; }
    [[nodiscard]] int recoveryOwnedShots() const noexcept { return recoveryOwnedShots_; }

private:
    int streak_ = 0;
    bool unavailable_ = false;
    int recoveryOwnedShots_ = 0;
    std::uint64_t lastCountedEpoch_ = 0;
    std::uint64_t sightedEpoch_ = 0;
    std::uint64_t lastRecoveryEpoch_ = 0;
};

} // namespace orion
