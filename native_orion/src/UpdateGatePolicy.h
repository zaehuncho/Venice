#pragma once

// [ORION_UPDATE_NO_LOCKOUT 2026-08-08] Silent-update decision, pure form.
//
// Live incident (deployed orion_native.log, 2026-08-07 05:11:34->05:13:24Z):
// the app relaunched itself 18 times in two minutes. Each cycle: the STARTUP
// gate correctly degraded because update 1.0.1 "was already attempted and did
// not apply" (per-version persisted latch, OrionAppController::
// updateAlreadyAttemptedThisVersion) -- but maybeAutoApplyUpdate() never
// consulted that latch, so the idle-session silent path immediately re-ran
// the exact same doomed handoff: startUpdate() -> quit -> OrionUpdater fails
// (unwritable install dir / manifest schema mismatch) -> relaunch -> repeat.
// A failed update must never be worse than no update: the automatic path gets
// ONE clean attempt per target version. A genuinely NEW version resets the
// latch (it is keyed by version) and auto-applies normally; a USER-initiated
// update (the explicit update button -> startUpdate()) intentionally bypasses
// this policy so a human can always retry after fixing the environment.
namespace orion {

[[nodiscard]] inline constexpr bool silentAutoApplyAllowed(
    bool updateAvailable,
    bool devBuild,
    bool updaterPresent,
    bool streamingActive,
    bool alreadyAttemptedThisVersion) noexcept
{
    return updateAvailable
        && !devBuild
        && updaterPresent
        && !streamingActive
        && !alreadyAttemptedThisVersion;
}

} // namespace orion
