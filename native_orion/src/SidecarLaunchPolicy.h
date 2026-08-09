#pragma once

namespace orion {

// Production packages intentionally omit the Python crown-jewel modules. The
// build-time production bit therefore dominates every runtime preference: a
// shipped launcher must use OrionSidecar.exe or fail closed. Development keeps
// the historical environment opt-in so source-backed iteration remains easy.
[[nodiscard]] constexpr bool shouldUseCompiledSidecar(bool productionBuild,
                                                       bool developmentOptIn) noexcept
{
    return productionBuild || developmentOptIn;
}

// Capture cards remain an independent, explicitly selected source. For the
// no-card Remote Play source, production must use the decoded-frame pipe; an
// inherited debug opt-out may never re-enable occlusion-prone window capture.
[[nodiscard]] constexpr bool shouldRequireRemotePlayFramePipe(bool productionBuild,
                                                              bool captureCardSource) noexcept
{
    return productionBuild && !captureCardSource;
}

// A capture-card launch is input-only from OrionStream's point of view. Even
// when a parent/debug environment happens to request the decoder pipe, exporting
// GPU frames would add a synchronous readback to every decoder callback for no
// consumer. Keep that work exclusive to an actual no-card decoder-pipe launch.
[[nodiscard]] constexpr bool shouldEnableRemotePlayFrameExport(
    bool captureCardSource,
    bool requiredFramePipe,
    bool developmentFramePipeRequested) noexcept
{
    return !captureCardSource && (requiredFramePipe || developmentFramePipeRequested);
}

// This is a gate ceiling, not a requested source cadence. It must sit above the
// nominal 60 Hz decoder callback so jitter cannot alias a 60 fps source down to
// roughly 30-40 fps. The producer remains source-capped at the decoded cadence.
inline constexpr int kRemotePlayFrameExportGateFps = 120;

// Window discovery performs EnumWindows plus process-image inspection. Poll it
// at frame cadence only during an explicit stream spawn/restart grace window;
// a passive capture-card preview has no stream window and must not keep that
// OS-wide scan on the GUI thread forever.
inline constexpr int kStreamWindowWatchdogFastMs = 16;
inline constexpr int kStreamWindowWatchdogSteadyMs = 250;

[[nodiscard]] constexpr int streamWindowWatchdogIntervalMs(
    long long nowMs, long long spawnGraceUntilMs) noexcept
{
    return spawnGraceUntilMs > 0 && nowMs <= spawnGraceUntilMs
        ? kStreamWindowWatchdogFastMs
        : kStreamWindowWatchdogSteadyMs;
}

} // namespace orion
