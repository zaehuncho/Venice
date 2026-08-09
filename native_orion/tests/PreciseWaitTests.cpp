// [ORION_PRECISE_WAIT] Offline validation of the precise-fire wait primitives
// (src/PreciseWaitTimer.h) and — as importantly — an in-process measurement of the mechanism
// they exist to defeat: condition_variable::wait_until quantizing to the process timer
// resolution. Run standalone, this binary prints three wakeup-error distributions
// (condvar @ ambient resolution, condvar @ timeBeginPeriod(1), hires timer @ ambient) plus the
// kernel timer resolution, so the 4-15.6ms live submit-lateness tail measured in
// logs/orion_native.log{,.1} can be reproduced and the fix's headroom quantified on any rig
// without a live session.
//
// Assertions are deliberately placed only on properties the high-resolution timer GUARANTEES
// (correct wake reason, precedence, and a generous latency envelope under TIME_CRITICAL); the
// ambient condvar legs are report-only because their behavior depends on machine state — that
// dependence is precisely the production bug.

#include <QtTest/QTest>

#include <QtCore/QOperatingSystemVersion>

#ifdef Q_OS_WIN

#include "PreciseWaitTimer.h"

#include <mmsystem.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <mutex>
#include <random>
#include <vector>

using orion::PreciseWaitTimer;
using Clock = std::chrono::steady_clock;

namespace {

struct Stats {
    double minMs = 0.0, medianMs = 0.0, p90Ms = 0.0, p99Ms = 0.0, maxMs = 0.0;
    double fracOverSpinLead = 0.0; // fraction the production 1.2ms spin could NOT absorb
};

Stats summarize(std::vector<double> errsMs)
{
    Stats s;
    if (errsMs.empty()) {
        return s;
    }
    std::sort(errsMs.begin(), errsMs.end());
    const auto pick = [&](double p) {
        const size_t i = std::min(errsMs.size() - 1,
                                  static_cast<size_t>(p * (errsMs.size() - 1) + 0.5));
        return errsMs[i];
    };
    s.minMs = errsMs.front();
    s.medianMs = pick(0.50);
    s.p90Ms = pick(0.90);
    s.p99Ms = pick(0.99);
    s.maxMs = errsMs.back();
    size_t over = 0;
    for (double e : errsMs) {
        if (e > 1.2) {
            ++over;
        }
    }
    s.fracOverSpinLead = static_cast<double>(over) / static_cast<double>(errsMs.size());
    return s;
}

void printStats(const char* label, const Stats& s, size_t n)
{
    std::printf("[precise-wait] %-34s n=%zu min=%.3f median=%.3f p90=%.3f p99=%.3f max=%.3f "
                "frac>1.2ms=%.1f%%\n",
                label, n, s.minMs, s.medianMs, s.p90Ms, s.p99Ms, s.maxMs,
                s.fracOverSpinLead * 100.0);
    std::fflush(stdout);
}

// Wakeup error of an unsignaled condvar wait_until — byte-identical mechanism to the fire
// worker's coarse leg (SleepConditionVariableSRW under MSVC STL).
std::vector<double> measureCondvar(int iterations, std::mt19937& rng)
{
    std::condition_variable cv;
    std::mutex m;
    std::uniform_real_distribution<double> targetMs(3.0, 18.0);
    std::vector<double> errs;
    errs.reserve(iterations);
    std::unique_lock<std::mutex> lock(m);
    for (int i = 0; i < iterations; ++i) {
        const auto target = Clock::now()
            + std::chrono::microseconds(static_cast<long long>(targetMs(rng) * 1000.0));
        while (Clock::now() < target) {
            cv.wait_until(lock, target);
        }
        errs.push_back(std::chrono::duration<double, std::milli>(Clock::now() - target).count());
    }
    return errs;
}

std::vector<double> measureHires(PreciseWaitTimer& timer, int iterations, std::mt19937& rng)
{
    std::uniform_real_distribution<double> targetMs(3.0, 18.0);
    std::vector<double> errs;
    errs.reserve(iterations);
    for (int i = 0; i < iterations; ++i) {
        const auto target = Clock::now()
            + std::chrono::microseconds(static_cast<long long>(targetMs(rng) * 1000.0));
        for (;;) {
            const auto r = timer.waitUntil(target, nullptr);
            if (Clock::now() >= target
                || r == PreciseWaitTimer::WaitResult::Failed) {
                break;
            }
        }
        errs.push_back(std::chrono::duration<double, std::milli>(Clock::now() - target).count());
    }
    return errs;
}

// The full production hybrid: hires wait to target-1.2ms, then the bounded YieldProcessor spin.
std::vector<double> measureHybrid(PreciseWaitTimer& timer, int iterations, std::mt19937& rng)
{
    constexpr auto kSpinLead = std::chrono::microseconds(1200);
    std::uniform_real_distribution<double> targetMs(3.0, 18.0);
    std::vector<double> errs;
    errs.reserve(iterations);
    for (int i = 0; i < iterations; ++i) {
        const auto target = Clock::now()
            + std::chrono::microseconds(static_cast<long long>(targetMs(rng) * 1000.0));
        while (Clock::now() + kSpinLead < target) {
            (void)timer.waitUntil(target - kSpinLead, nullptr);
        }
        while (Clock::now() < target) {
            YieldProcessor();
        }
        errs.push_back(std::chrono::duration<double, std::milli>(Clock::now() - target).count());
    }
    return errs;
}

class RaiiThreadPriority {
public:
    explicit RaiiThreadPriority(int priority)
        : previous_(GetThreadPriority(GetCurrentThread()))
    {
        SetThreadPriority(GetCurrentThread(), priority);
    }
    ~RaiiThreadPriority() { SetThreadPriority(GetCurrentThread(), previous_); }

private:
    int previous_;
};

} // namespace

class PreciseWaitTests : public QObject {
    Q_OBJECT

private slots:
    void hiresTimerCreates()
    {
        PreciseWaitTimer timer;
        QVERIFY2(timer.create(),
                 "CREATE_WAITABLE_TIMER_HIGH_RESOLUTION unavailable — the "
                 "ORION_PRECISE_WAIT_HIRES path would silently fall back on this OS");
        QVERIFY(timer.valid());
    }

    void pastTargetReturnsImmediately()
    {
        PreciseWaitTimer timer;
        QVERIFY(timer.create());
        const auto t0 = Clock::now();
        const auto r = timer.waitUntil(t0 - std::chrono::milliseconds(5), nullptr);
        QCOMPARE(static_cast<int>(r), static_cast<int>(PreciseWaitTimer::WaitResult::TimerFired));
        const double elapsedMs =
            std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        QVERIFY(elapsedMs < 5.0);
    }

    void wakeEventTakesPrecedenceOverDueTime()
    {
        PreciseWaitTimer timer;
        QVERIFY(timer.create());
        HANDLE wake = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        QVERIFY(wake != nullptr);
        SetEvent(wake); // latched before the wait, exactly like an arm-side SetEvent
        const auto t0 = Clock::now();
        const auto r = timer.waitUntil(t0 + std::chrono::milliseconds(250), wake);
        const double elapsedMs =
            std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        CloseHandle(wake);
        QCOMPARE(static_cast<int>(r), static_cast<int>(PreciseWaitTimer::WaitResult::Signaled));
        QVERIFY2(elapsedMs < 100.0, "latched wake event must end the wait, not the due time");
    }

    // The core measurement. Report-only for the condvar legs; asserted envelope for the hires
    // legs, measured at the fire worker's own priority (TIME_CRITICAL).
    void wakeupErrorDistributions()
    {
        constexpr int kIterations = 150;
        std::mt19937 rng(20260806);
        RaiiThreadPriority prio(THREAD_PRIORITY_TIME_CRITICAL);

        std::printf("[precise-wait] kernel timer resolution (ambient): %.3f ms\n",
                    orion::currentTimerResolutionMs());

        const auto condvarAmbient = measureCondvar(kIterations, rng);
        printStats("condvar @ ambient resolution", summarize(condvarAmbient), condvarAmbient.size());

        timeBeginPeriod(1);
        std::printf("[precise-wait] kernel timer resolution (timeBeginPeriod(1)): %.3f ms\n",
                    orion::currentTimerResolutionMs());
        const auto condvar1ms = measureCondvar(kIterations, rng);
        printStats("condvar @ timeBeginPeriod(1)", summarize(condvar1ms), condvar1ms.size());
        timeEndPeriod(1);

        PreciseWaitTimer timer;
        QVERIFY(timer.create());
        const auto hires = measureHires(timer, kIterations, rng);
        const Stats hiresStats = summarize(hires);
        printStats("hires timer @ ambient resolution", hiresStats, hires.size());

        const auto hybrid = measureHybrid(timer, kIterations, rng);
        const Stats hybridStats = summarize(hybrid);
        printStats("hybrid (hires + 1.2ms spin)", hybridStats, hybrid.size());

        // The guarantees the fix rests on, with headroom for a loaded machine:
        //  - the hires wake must not depend on the ambient timer resolution (median far below
        //    the 15.625ms quantum, and inside the production 1.2ms spin lead most of the time);
        //  - the full hybrid must land sub-2ms even at p99 (live tail today reaches 15.5ms).
        QVERIFY2(hiresStats.medianMs < 2.5,
                 qPrintable(QStringLiteral("hires median %1 ms").arg(hiresStats.medianMs)));
        QVERIFY2(hiresStats.p90Ms < 5.0,
                 qPrintable(QStringLiteral("hires p90 %1 ms").arg(hiresStats.p90Ms)));
        QVERIFY2(hybridStats.p99Ms < 2.0,
                 qPrintable(QStringLiteral("hybrid p99 %1 ms").arg(hybridStats.p99Ms)));
    }

    void throttleOptOutApplies()
    {
        const bool ok = orion::disableTimerResolutionThrottling();
        std::printf("[precise-wait] timer-resolution throttle opt-out: %s\n",
                    ok ? "applied" : "unavailable");
        std::fflush(stdout);
        // Windows 11 (build 22000+) supports PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION;
        // assert only there so the suite stays green on a Win10 build agent.
        const auto v = QOperatingSystemVersion::current();
        if (v.majorVersion() > 10
            || (v.majorVersion() == 10 && v.microVersion() >= 22000)) {
            QVERIFY2(ok, "SetProcessInformation(ProcessPowerThrottling, IGNORE_TIMER_RESOLUTION) "
                         "failed on a Windows 11 host");
        }
    }
};

QTEST_APPLESS_MAIN(PreciseWaitTests)

#include "PreciseWaitTests.moc"

#else // !Q_OS_WIN

class PreciseWaitTests : public QObject {
    Q_OBJECT
private slots:
    void skippedOffWindows() { QSKIP("precise-wait primitives are Windows-only"); }
};

QTEST_APPLESS_MAIN(PreciseWaitTests)

#include "PreciseWaitTests.moc"

#endif
