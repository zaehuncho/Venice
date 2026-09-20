#pragma once
#include <QtCore/QString>
#ifdef Q_OS_WIN
#include <windows.h>
#include <cstdio>
#include <iterator>

namespace orion::native_crash_diagnostics {
inline wchar_t logPath[32768]{};
inline LPTOP_LEVEL_EXCEPTION_FILTER previousFilter = nullptr;
inline volatile LONG recording = 0;

// Minimal unhandled-fault breadcrumb, not a memory dump: no settings, input,
// account data or process environment is recorded from the faulting process.
inline LONG WINAPI recordUnhandled(EXCEPTION_POINTERS* info)
{
    if (info && info->ExceptionRecord && logPath[0]
            && InterlockedCompareExchange(&recording, 1, 0) == 0) {
        const auto* record = info->ExceptionRecord;
        SYSTEMTIME now{};
        GetSystemTime(&now);
        char line[256]{};
        const int length = sprintf_s(line,
            "%04u-%02u-%02uT%02u:%02u:%02uZ pid=%lu exception=0x%08lx address=%p\r\n",
            now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond,
            GetCurrentProcessId(), record->ExceptionCode, record->ExceptionAddress);
        const HANDLE file = CreateFileW(logPath, FILE_APPEND_DATA,
            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file != INVALID_HANDLE_VALUE) {
            DWORD written = 0;
            if (length > 0) WriteFile(file, line, static_cast<DWORD>(length), &written, nullptr);
            CloseHandle(file);
        }
    }
    return previousFilter ? previousFilter(info) : EXCEPTION_CONTINUE_SEARCH;
}
inline void install(const QString& path)
{
    if (path.isEmpty() || path.size() >= static_cast<qsizetype>(std::size(logPath))) return;
    path.toWCharArray(logPath);
    logPath[path.size()] = 0;
    const auto previous = SetUnhandledExceptionFilter(&recordUnhandled);
    if (previous != &recordUnhandled) previousFilter = previous;
}
} // namespace orion::native_crash_diagnostics
#else
namespace orion::native_crash_diagnostics {
inline void install(const QString&) {}
}
#endif
