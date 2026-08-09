#pragma once

// Deterministic shutdown state shared by the launcher and unit tests. Window
// close, aboutToQuit, and destruction can all enter cleanup; only one caller is
// allowed to own process and controller teardown.

namespace orion {

enum class ApplicationShutdownPhase {
    Running,
    Requested,
    Teardown,
    Complete,
};

[[nodiscard]] inline constexpr bool applicationShutdownActive(
    ApplicationShutdownPhase phase) noexcept
{
    return phase != ApplicationShutdownPhase::Running;
}

[[nodiscard]] inline constexpr bool markApplicationShutdownRequested(
    ApplicationShutdownPhase& phase) noexcept
{
    if (phase != ApplicationShutdownPhase::Running) {
        return false;
    }
    phase = ApplicationShutdownPhase::Requested;
    return true;
}

[[nodiscard]] inline constexpr bool beginApplicationShutdownTeardown(
    ApplicationShutdownPhase& phase) noexcept
{
    if (phase == ApplicationShutdownPhase::Teardown
        || phase == ApplicationShutdownPhase::Complete) {
        return false;
    }
    phase = ApplicationShutdownPhase::Teardown;
    return true;
}

inline constexpr void completeApplicationShutdownTeardown(
    ApplicationShutdownPhase& phase) noexcept
{
    phase = ApplicationShutdownPhase::Complete;
}

[[nodiscard]] inline constexpr bool shouldResumeRuntimeAfterDisconnect(
    ApplicationShutdownPhase phase) noexcept
{
    return phase == ApplicationShutdownPhase::Running;
}

// QWindow::visible remains true while minimized. It becomes false when the
// frameless root is hidden/closed, which is the lifecycle fallback we need when
// a hidden popup prevents lastWindowClosed from ending the process.
[[nodiscard]] inline constexpr bool shouldRequestShutdownForRootVisibility(
    bool visible) noexcept
{
    return !visible;
}

} // namespace orion
