// [RT-MED-10 / CL3-F8-007 2026-09-23] GUI-freeze watchdog decision (GuiFreezeWatchdogPolicy.h).
// Sleep/resume and clock steps must never read as a GUI freeze; a real GUI stall still trips.

#include "GuiFreezeWatchdogPolicy.h"

#include <QtTest/QtTest>

namespace gf = orion::gui_freeze;

class GuiFreezeWatchdogPolicyTests final : public QObject {
    Q_OBJECT

private slots:
    void freshHeartbeatIsHealthy()
    {
        gf::LoopState s;
        QVERIFY(gf::evaluate(s, 1000, 900, 0, false) == gf::Verdict::Healthy);
        QVERIFY(gf::evaluate(s, 1500, 1400, 0, false) == gf::Verdict::Healthy);
    }

    void aRealGuiStallStillTrips()
    {
        // The watchdog loop keeps running every 500 ms while the GUI heartbeat is stuck at 0.
        gf::LoopState s;
        gf::Verdict v = gf::Verdict::Healthy;
        qint64 now = 0;
        for (; now <= gf::kFreezeTripMs; now += gf::kPollIntervalMs) {
            v = gf::evaluate(s, now, 0, 0, false);
            QVERIFY(v == gf::Verdict::Healthy);
        }
        v = gf::evaluate(s, now, 0, 0, false);   // 6500 ms stale, loop on time
        QVERIFY(v == gf::Verdict::Trip);
    }

    void anHourOfSleepIsASuspendNotAFreeze()
    {
        gf::LoopState s;
        QVERIFY(gf::evaluate(s, 10'000, 9'900, 0, false) == gf::Verdict::Healthy);
        // Whole process paused: the watchdog's own loop resumes 3600 s later.
        QVERIFY(gf::evaluate(s, 10'000 + 3'600'000, 9'900, 0, false) == gf::Verdict::Suspended);
        // Caller re-seeds the heartbeat to now; the next poll is healthy.
        const qint64 resumed = 10'000 + 3'600'000;
        QVERIFY(gf::evaluate(s, resumed + 500, resumed, 0, false) == gf::Verdict::Healthy);
    }

    void announcedSuspendParksTheWatchdog()
    {
        gf::LoopState s;
        QVERIFY(gf::evaluate(s, 0, 0, 0, false) == gf::Verdict::Healthy);
        // PBT_APMSUSPEND arrived and the GUI then stopped: still no trip.
        QVERIFY(gf::evaluate(s, 7'000, 0, 0, true) == gf::Verdict::Suspended);
    }

    void aBackwardStepNeverTripsOrBlinds()
    {
        gf::LoopState s;
        QVERIFY(gf::evaluate(s, 100'000, 99'900, 0, false) == gf::Verdict::Healthy);
        QVERIFY(gf::evaluate(s, 40'000, 99'900, 0, false) == gf::Verdict::Suspended);
        // After the re-seed the watchdog works normally again (not blinded).
        gf::Verdict v = gf::Verdict::Healthy;
        for (qint64 now = 40'500; now <= 40'000 + gf::kFreezeTripMs + 1'000; now += gf::kPollIntervalMs) {
            v = gf::evaluate(s, now, 40'000, 0, false);
        }
        QVERIFY(v == gf::Verdict::Trip);
    }

    void declaredTeardownBlockIsSuppressed()
    {
        gf::LoopState s;
        QVERIFY(gf::evaluate(s, 0, 0, 20'000, false) == gf::Verdict::Suppressed);
        QVERIFY(gf::evaluate(s, 500, 0, 20'000, false) == gf::Verdict::Suppressed);
    }

    void monotonicClockNeverGoesBackwards()
    {
        const qint64 a = gf::monotonicMs();
        const qint64 b = gf::monotonicMs();
        QVERIFY(b >= a);
    }
};

QTEST_GUILESS_MAIN(GuiFreezeWatchdogPolicyTests)
#include "GuiFreezeWatchdogPolicyTests.moc"
