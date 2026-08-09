#pragma once

// [ORION_PRECISE_WAIT] High-resolution waitable-timer wait for the precise-fire worker, plus the
// Windows 11 timer-resolution throttling opt-out.
//
// WHY THIS EXISTS (measured 2026-08-06 over logs/orion_native.log{,.1}, 786 precise-thread
// releases): the worker's coarse leg used condition_variable::wait_until, which on Windows rides
// SleepConditionVariableSRW and therefore quantizes to the PROCESS timer resolution. The worker
// does call timeBeginPeriod(1), but starting with Windows 11 the kernel IGNORES a process's
// timer-resolution request while its windows are minimized or fully occluded unless the process
// opts out via PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION. The measured submit error
// (Scheduled fire: deltaMs = actual submit - armed deadline) is bimodal exactly as that predicts:
// 74% sub-millisecond, and a ~15% tail spread nearly uniformly from 4ms up to a hard ceiling at
// ~15.6ms (the default quantum), clustered by SESSION (whole hours show zero tail events, other
// hours are 50-68% tail). The same quantum explains the subtick_fire_at_past token kills
// (n=47, overdue 8.2-21.5ms vs the 8ms grace): the worker overslept its own deadline and the
// engine's payload mirror killed the token fail-closed before it could submit.
//
// CREATE_WAITABLE_TIMER_HIGH_RESOLUTION timers fire at precise due times regardless of the
// current process timer resolution, so waiting on one removes the dependency on the throttling
// policy entirely. Both facilities here are consumed behind default-OFF env flags:
//   ORION_PRECISE_WAIT_HIRES=1  -> the fire worker's coarse leg waits on this timer instead of
//                                  the condvar (same predicate loop, same 1.2ms spin tail).
//   ORION_TIMER_RES_GUARD=1     -> the process opts out of timer-resolution throttling so the
//                                  existing timeBeginPeriod(1) request is honored even occluded.
//
// FAIL-CLOSED: nothing here can make a submit happen; both facilities only change when an
// already-armed worker WAKES. A broken timer degrades to a bounded WaitForMultipleObjects
// timeout and the caller's predicate loop re-evaluates exactly as it does for a condvar
// spurious wake. Timer creation failure reports invalid and callers keep the condvar path.

#ifdef _WIN32

#include <windows.h>

#include <chrono>

#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif

namespace orion {

class PreciseWaitTimer {
public:
    enum class WaitResult {
        TimerFired,       // the due time was reached (normal wake)
        Signaled,         // the caller's cancel/wake event was signaled first
        TimedOutFallback, // paranoid backstop expired (timer misbehaved); re-check and re-wait
        Failed,           // wait machinery itself failed; caller should use its fallback path
    };

    PreciseWaitTimer() = default;
    PreciseWaitTimer(const PreciseWaitTimer&) = delete;
    PreciseWaitTimer& operator=(const PreciseWaitTimer&) = delete;

    ~PreciseWaitTimer()
    {
        if (timer_) {
            CloseHandle(timer_);
        }
    }

    // Attempt to create the high-resolution timer. Returns false (and stays invalid) on
    // platforms/builds where CREATE_WAITABLE_TIMER_HIGH_RESOLUTION is unsupported.
    bool create()
    {
        if (timer_) {
            return true;
        }
        timer_ = CreateWaitableTimerExW(
            nullptr, nullptr,
            CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
            TIMER_ALL_ACCESS);
        return timer_ != nullptr;
    }

    [[nodiscard]] bool valid() const { return timer_ != nullptr; }

    // Block until `target` (steady_clock) or until `wake` is signaled, whichever comes first.
    // `wake` takes precedence when both are signaled (WaitForMultipleObjects returns the
    // lowest-index signaled handle), so a disarm/quit can never be masked by the due time.
    // A target at or before now returns TimerFired immediately without arming the timer.
    WaitResult waitUntil(std::chrono::steady_clock::time_point target, HANDLE wake)
    {
        if (!timer_) {
            return WaitResult::Failed;
        }
        const auto now = std::chrono::steady_clock::now();
        if (target <= now) {
            return WaitResult::TimerFired;
        }
        const auto remaining =
            std::chrono::duration_cast<std::chrono::nanoseconds>(target - now);
        // Negative due time = relative, in 100ns units.
        LARGE_INTEGER due;
        due.QuadPart = -static_cast<LONGLONG>((remaining.count() + 99) / 100);
        if (!SetWaitableTimer(timer_, &due, 0, nullptr, nullptr, FALSE)) {
            return WaitResult::Failed;
        }
        // Paranoid backstop: the timer SHOULD fire at `due`; if the machinery misbehaves the
        // wait still returns bounded (remaining + 50ms) and the caller's loop re-checks time.
        const DWORD backstopMs = static_cast<DWORD>(
            remaining.count() / 1'000'000 + 50);
        HANDLE handles[2];
        DWORD count = 0;
        if (wake) {
            handles[count++] = wake;
        }
        handles[count++] = timer_;
        const DWORD rc = WaitForMultipleObjects(count, handles, FALSE, backstopMs);
        if (rc == WAIT_TIMEOUT) {
            return WaitResult::TimedOutFallback;
        }
        if (rc == WAIT_FAILED) {
            return WaitResult::Failed;
        }
        const DWORD index = rc - WAIT_OBJECT_0;
        if (wake && index == 0) {
            return WaitResult::Signaled;
        }
        return WaitResult::TimerFired;
    }

private:
    HANDLE timer_ = nullptr;
};

// Opt this process out of Windows 11 timer-resolution throttling so its timeBeginPeriod(1)
// request keeps being honored while the window is minimized/occluded. ControlMask set +
// StateMask cleared = "always honor this process's timer resolution requests" (setting the
// StateMask bit would do the opposite). Returns false when the OS/SDK path is unavailable;
// harmless no-op on Windows 10 where the throttling policy does not exist.
inline bool disableTimerResolutionThrottling()
{
#ifdef PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
    PROCESS_POWER_THROTTLING_STATE state{};
    state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION;
    state.ControlMask = PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION;
    state.StateMask = 0;
    return SetProcessInformation(GetCurrentProcess(), ProcessPowerThrottling,
                                 &state, sizeof(state)) != FALSE;
#else
    // SDK too old to know the policy; nothing to opt out of at compile time.
    return false;
#endif
}

// Current kernel timer resolution in milliseconds (what Sleep/condvar timeouts actually
// quantize to for THIS process right now), via NtQueryTimerResolution. Returns -1.0 when
// unavailable. Diagnostic only — used by tests and preflight reporting.
inline double currentTimerResolutionMs()
{
    typedef LONG(NTAPI * NtQueryTimerResolutionFn)(PULONG, PULONG, PULONG);
    const HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    if (!ntdll) {
        return -1.0;
    }
    const auto fn = reinterpret_cast<NtQueryTimerResolutionFn>(
        GetProcAddress(ntdll, "NtQueryTimerResolution"));
    if (!fn) {
        return -1.0;
    }
    ULONG minRes = 0, maxRes = 0, curRes = 0;
    if (fn(&minRes, &maxRes, &curRes) != 0) {
        return -1.0;
    }
    return static_cast<double>(curRes) / 10000.0; // 100ns units -> ms
}

} // namespace orion

#endif // _WIN32
