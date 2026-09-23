#pragma once

// [RT-MED-10 / CL3-F8-007 2026-09-23] GUI-freeze watchdog decision, header-only so it is
// unit-testable without the controller.
//
// The old watchdog compared QDateTime::currentMSecsSinceEpoch() stamps. After a laptop or
// desktop sleep, the first 500 ms poll saw a "freeze" as long as the whole sleep, disarmed,
// and the next GUI tick latched SAFE MODE ("UI thread froze for over 6 seconds") even when
// Venice sat idle. Each sleep also spent one of the per-session auto-recoveries, and a wall
// clock step (NTP, manual change) could trip or blind it too.
//
// Now:
//   * both stamps use a steady (monotonic) clock, so wall-clock steps do nothing;
//   * a suspend announced by Windows (WM_POWERBROADCAST PBT_APMSUSPEND) parks the watchdog
//     until the resume message re-seeds the heartbeat;
//   * and, independent of that message, a gap in the WATCHDOG'S OWN 500 ms loop longer than
//     kLoopGapSuspendMs means the whole process was paused (sleep, hibernate, a VM pause). A
//     genuine GUI freeze leaves the watchdog thread running on time, so it still trips.

#include <QtCore/QtGlobal>

#include <chrono>

namespace orion::gui_freeze {

inline constexpr qint64 kFreezeTripMs = 6000;
inline constexpr qint64 kPollIntervalMs = 500;
// Six missed polls. A GUI freeze never delays the watchdog's own thread this long.
inline constexpr qint64 kLoopGapSuspendMs = 3000;

[[nodiscard]] inline qint64 monotonicMs() noexcept
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

enum class Verdict {
    Healthy,      // nothing to do
    Suspended,    // process/system was paused: re-seed the heartbeat to now, never trip
    Suppressed,   // inside a declared bounded blocking section (disconnect teardown)
    Trip,         // the GUI thread really stopped for longer than kFreezeTripMs
};

struct LoopState {
    qint64 lastLoopMs = -1;
};

// Called once per watchdog loop iteration with the current monotonic time.
[[nodiscard]] inline Verdict evaluate(LoopState& state, qint64 nowMs, qint64 heartbeatMs,
                                      qint64 suppressUntilMs, bool systemSuspended) noexcept
{
    const qint64 loopGap = state.lastLoopMs < 0 ? 0 : nowMs - state.lastLoopMs;
    state.lastLoopMs = nowMs;
    if (systemSuspended || loopGap > kLoopGapSuspendMs || loopGap < 0) {
        return Verdict::Suspended;
    }
    if (nowMs < suppressUntilMs) {
        return Verdict::Suppressed;
    }
    if (nowMs - heartbeatMs > kFreezeTripMs) {
        return Verdict::Trip;
    }
    return Verdict::Healthy;
}

} // namespace orion::gui_freeze
