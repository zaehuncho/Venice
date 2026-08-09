#pragma once

#include <QtCore/qglobal.h>

#ifdef Q_OS_WIN

#include <QtCore/QString>

#include <Windows.h>

namespace orion::deep_link_target_policy {

// Resolve both paths through Windows' final-path API and compare them with
// Windows ordinal, case-insensitive semantics. Any resolution failure rejects
// the match so callers never fall back to comparing attacker-controlled text.
[[nodiscard]] bool canonicalExecutablePathsEqual(
    const QString& left,
    const QString& right);

// Resolve a process image using a query-only handle and require it to be the
// exact executable at expectedExecutablePath.
[[nodiscard]] bool processImageMatchesExecutable(
    DWORD processId,
    const QString& expectedExecutablePath);

// Bind a top-level HWND to the process image above. verifiedProcessId is
// populated only after the HWND/PID association survives a post-check.
[[nodiscard]] bool windowOwnedByExecutable(
    HWND window,
    const QString& expectedExecutablePath,
    DWORD* verifiedProcessId = nullptr);

} // namespace orion::deep_link_target_policy

#endif // Q_OS_WIN
