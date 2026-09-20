#pragma once

// [PRESSED OVERLAY 2026-08-08] Raw physical Square-held latch backing the local
// "PRESSED" input-acknowledgment badge (owner request, 2026-08-08).
//
// The input hook forwards a physical Square press to the console undelayed, but
// the visible meter feedback lags by V + meter_delay ms. The badge exists so the
// human gets confirmation on the SAME input-hook tick the press is written —
// perceived responsiveness decoupled from the video pipeline.
//
// Contract:
//  - update() is fed the raw held bit once per selected-device poll (the same
//    report that feeds MeterDelayController::setPhysicalSquareHeld and mints
//    physical shot epochs). It returns true exactly when the visible state
//    changed ON THIS CALL — synchronous, never deferred to a later tick.
//  - No debounce on purpose: this mirrors the raw button. The release-side
//    smoothing is cosmetic and lives entirely in QML (~150 ms fade), where a
//    one-poll transport blip costs at most a restarted fade.
//  - PASSIVE by construction: never consumes or rewrites the report, carries no
//    shot/fire authority, sends nothing to the console, and is not part of the
//    meter-box/detection overlay family.
//  - The caller must feed update(false) when the physical route drops
//    (!selected.active) so a pad unplugged mid-hold cannot leave the badge lit.

namespace orion {

class SquareHeldLatch final {
public:
    // Feed the raw physical Square-held bit each input-poll tick. Returns true
    // when the held state changed on this very call (rising OR falling edge).
    [[nodiscard]] bool update(bool held) noexcept
    {
        if (held == held_) {
            return false;
        }
        held_ = held;
        return true;
    }

    [[nodiscard]] bool held() const noexcept { return held_; }

private:
    bool held_ = false;
};


// Passive release-side forensic observer.  Raw UP and the third consecutive
// selected-device UP are separately observable because ownership can deliberately
// hold output through the first two polls.  This never changes input or grants
// a shot epoch.  Route loss resets it rather than pretending a missing report is UP.
enum class SquareUpAuditPhase { None, RawUp, DebouncedUp };

class SquareUpAuditTracker final {
public:
    [[nodiscard]] SquareUpAuditPhase observe(bool held) noexcept
    {
        if (held) {
            heldSeen_ = true;
            upPolls_ = 0;
            return SquareUpAuditPhase::None;
        }
        if (!heldSeen_ || upPolls_ >= 3) {
            return SquareUpAuditPhase::None;
        }
        ++upPolls_;
        return upPolls_ == 1 ? SquareUpAuditPhase::RawUp
            : (upPolls_ == 3 ? SquareUpAuditPhase::DebouncedUp
                            : SquareUpAuditPhase::None);
    }

    void reset() noexcept { heldSeen_ = false; upPolls_ = 0; }

private:
    bool heldSeen_ = false;
    int upPolls_ = 0;
};

} // namespace orion
