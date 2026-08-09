#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  WinDivertApi.h — runtime (dynamic) linkage to WinDivert64.dll
// ───────────────────────────────────────────────────────────────────────────
//
//  There is NO WinDivert.h and NO WinDivert import library anywhere in the tree
//  (only the LGPL-clean WinDivert64.dll / WinDivert64.sys pair under
//  vendor/windivert/, exports verified: WinDivertOpen/Recv/Send/Close/Shutdown/
//  HelperCompileFilter, i.e. the WinDivert 2.x C ABI). To "dynamic-link
//  WinDivert64.dll" (deliverable) without inventing a header/lib, this loader
//  resolves the handful of entry points the service needs via LoadLibrary +
//  GetProcAddress at runtime.
//
//  Consequences that matter for the wave-1 hazards:
//    * The DLL is loaded LAZILY, only inside the service, only when the
//      intercept/sniff actually opens. A machine with no driver simply fails the
//      load and the service reports driver_state honestly (hazard 1 / hazard 7).
//    * WINDIVERT_ADDRESS is treated as an opaque 64-byte blob. The service never
//      inspects or mutates it — recv() fills it, send() replays it verbatim, so
//      the exact 2.x struct layout is irrelevant and cannot rot.
//    * Nothing here opens a handle. Opening is done by MeterDelayIntercept /
//      the sniff loop, which run ONLY in the LocalSystem service (hazard 1).
//
//  This header is Win32-only and pulls in <windows.h>; it is never included by a
//  unit test (tests must not touch the kernel driver — task constraint).

#ifdef _WIN32

#include <windows.h>

#include <cstdint>
#include <string>

namespace venicenet {

// WinDivert 2.x WINDIVERT_ADDRESS is 64 bytes. We only pass it through recv/send
// unchanged, so an opaque fixed-size buffer is both sufficient and layout-proof.
inline constexpr size_t kWinDivertAddrSize = 64;

// WINDIVERT_LAYER values (WinDivert 2.x).
enum WinDivertLayer : int {
    WINDIVERT_LAYER_NETWORK = 0,
    WINDIVERT_LAYER_NETWORK_FORWARD = 1,
    WINDIVERT_LAYER_FLOW = 2,
    WINDIVERT_LAYER_SOCKET = 3,
    WINDIVERT_LAYER_REFLECT = 4,
};

// WINDIVERT_FLAG_* bits.
inline constexpr uint64_t WINDIVERT_FLAG_SNIFF = 0x0001;
inline constexpr uint64_t WINDIVERT_FLAG_DROP = 0x0002;
inline constexpr uint64_t WINDIVERT_FLAG_RECV_ONLY = 0x0004;
inline constexpr uint64_t WINDIVERT_FLAG_SEND_ONLY = 0x0008;
inline constexpr uint64_t WINDIVERT_FLAG_NO_INSTALL = 0x0010;
inline constexpr uint64_t WINDIVERT_FLAG_FRAGMENTS = 0x0020;

// WINDIVERT_SHUTDOWN values.
enum WinDivertShutdownHow : int {
    WINDIVERT_SHUTDOWN_RECV = 1,
    WINDIVERT_SHUTDOWN_SEND = 2,
    WINDIVERT_SHUTDOWN_BOTH = 3,
};

// Resolves the WinDivert entry points once and hands out the function pointers.
// One instance per process is intended; the service owns exactly one.
class WinDivertLibrary {
public:
    using OpenFn = HANDLE(WINAPI*)(const char* filter, int layer, INT16 priority, UINT64 flags);
    using RecvFn = BOOL(WINAPI*)(HANDLE handle, void* pPacket, UINT packetLen, UINT* pRecvLen,
                                 void* pAddr);
    using SendFn = BOOL(WINAPI*)(HANDLE handle, const void* pPacket, UINT packetLen, UINT* pSendLen,
                                 const void* pAddr);
    using CloseFn = BOOL(WINAPI*)(HANDLE handle);
    using ShutdownFn = BOOL(WINAPI*)(HANDLE handle, int how);
    using CompileFilterFn = BOOL(WINAPI*)(const char* filter, int layer, char* object,
                                          UINT objLen, const char** errorStr, UINT* errorPos);

    WinDivertLibrary() = default;
    WinDivertLibrary(const WinDivertLibrary&) = delete;
    WinDivertLibrary& operator=(const WinDivertLibrary&) = delete;
    ~WinDivertLibrary()
    {
        if (module_) {
            ::FreeLibrary(module_);
        }
    }

    // Attempt to load WinDivert64.dll from, in order: the service exe's own
    // directory, an adjacent windivert/ subdir, and vendor/windivert/. Returns
    // false and records lastError() when the DLL cannot be found/loaded (e.g. an
    // install that never staged it). Never throws.
    bool load(const std::wstring& exeDir)
    {
        if (module_) {
            return true;
        }
        const wchar_t* names[] = {L"WinDivert64.dll", L"WinDivert.dll"};
        const std::wstring dirs[] = {
            exeDir,
            exeDir + L"\\windivert",
            exeDir + L"\\vendor\\windivert",
        };
        for (const std::wstring& dir : dirs) {
            for (const wchar_t* name : names) {
                std::wstring full = dir + L"\\" + name;
                if (::GetFileAttributesW(full.c_str()) == INVALID_FILE_ATTRIBUTES) {
                    continue;
                }
                // Load with the DLL's own directory on the search path so the
                // .sys next to it resolves.
                module_ = ::LoadLibraryExW(full.c_str(), nullptr,
                                           LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
                                               | LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR);
                if (!module_) {
                    module_ = ::LoadLibraryW(full.c_str());
                }
                if (module_) {
                    if (resolve()) {
                        return true;
                    }
                    ::FreeLibrary(module_);
                    module_ = nullptr;
                    lastError_ = "WinDivert64.dll is missing an expected export";
                    return false;
                }
            }
        }
        lastError_ = "WinDivert64.dll not found beside the service or under vendor/windivert";
        return false;
    }

    bool loaded() const { return module_ != nullptr; }
    const std::string& lastError() const { return lastError_; }

    OpenFn open = nullptr;
    RecvFn recv = nullptr;
    SendFn send = nullptr;
    CloseFn close = nullptr;
    ShutdownFn shutdown = nullptr;
    CompileFilterFn compileFilter = nullptr;

private:
    bool resolve()
    {
        open = reinterpret_cast<OpenFn>(proc("WinDivertOpen"));
        recv = reinterpret_cast<RecvFn>(proc("WinDivertRecv"));
        send = reinterpret_cast<SendFn>(proc("WinDivertSend"));
        close = reinterpret_cast<CloseFn>(proc("WinDivertClose"));
        shutdown = reinterpret_cast<ShutdownFn>(proc("WinDivertShutdown"));
        compileFilter = reinterpret_cast<CompileFilterFn>(proc("WinDivertHelperCompileFilter"));
        return open && recv && send && close && compileFilter;
    }
    FARPROC proc(const char* name) const { return ::GetProcAddress(module_, name); }

    HMODULE module_ = nullptr;
    std::string lastError_;
};

} // namespace venicenet

#endif // _WIN32
