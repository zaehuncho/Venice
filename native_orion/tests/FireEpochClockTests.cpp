#include "../src/FireEpochClock.h"

#include <QtTest/QtTest>

#include <chrono>
#include <cmath>

using namespace orion;

class FireEpochClockTests final : public QObject {
    Q_OBJECT

private slots:
    void ackCompletionDelayDoesNotMoveCommandAuthority_data()
    {
        QTest::addColumn<double>("ackDelayMs");
        QTest::newRow("immediate") << 0.0;
        QTest::newRow("ten-ms") << 10.0;
        QTest::newRow("forty-ms") << 40.0;
    }

    void ackCompletionDelayDoesNotMoveCommandAuthority()
    {
        QFETCH(double, ackDelayMs);
        constexpr double commandIssuedMs = 2500.25;
        constexpr double deadlineMs = 2499.75;
        double fakeNowMs = commandIssuedMs;
        int dispatchCalls = 0;
        const PreciseFireDispatchTiming timing =
            timePreciseFireDispatch(
                [&] { return fakeNowMs; },
                [&] {
                    ++dispatchCalls;
                    fakeNowMs += ackDelayMs;
                });

        QVERIFY(timing.valid());
        QCOMPARE(dispatchCalls, 1);
        QCOMPARE(timing.commandIssuedMs, commandIssuedMs);
        QCOMPARE(timing.activeRouteCompleteMs,
                 commandIssuedMs + ackDelayMs);
        QCOMPARE(timing.activeRouteDurationMs, ackDelayMs);
        // This is the exact value AutomationEngine::confirmScheduledFire
        // receives. ACK delay must not appear as scheduler lateness.
        QCOMPARE(timing.commandIssuedMs - deadlineMs, 0.5);
    }

    void epochMappingUsesCommandInstantWhenAckCompletesLater_data()
    {
        QTest::addColumn<double>("ackDelayMs");
        QTest::newRow("immediate") << 0.0;
        QTest::newRow("ten-ms") << 10.0;
        QTest::newRow("forty-ms") << 40.0;
    }

    void epochMappingUsesCommandInstantWhenAckCompletesLater()
    {
        QFETCH(double, ackDelayMs);
        constexpr double commandIssuedMs = 5000.0;
        constexpr qint64 commandEpochMs = 1'700'000'000'000;
        const PreciseFireDispatchTiming timing =
            finalizePreciseFireDispatchTiming(
                commandIssuedMs, commandIssuedMs + ackDelayMs);

        // Model captureFireEpochMs sampling wall time after the ACK. Both
        // monotonic and wall clocks advanced by ackDelayMs, so aligning the
        // original command instant must remain byte-for-byte invariant.
        const double mapped = alignFireEpochMs(
            timing.commandIssuedMs,
            timing.activeRouteCompleteMs,
            commandEpochMs + static_cast<qint64>(ackDelayMs),
            timing.activeRouteCompleteMs);
        QCOMPARE(mapped, static_cast<double>(commandEpochMs));
    }

    void invalidDeliveryCompletionDoesNotReplaceCommandInstant()
    {
        const PreciseFireDispatchTiming timing =
            finalizePreciseFireDispatchTiming(25.0, 24.0);
        QCOMPARE(timing.commandIssuedMs, 25.0);
        QCOMPARE(timing.activeRouteCompleteMs, -1.0);
        QCOMPARE(timing.activeRouteDurationMs, -1.0);
        QVERIFY(!timing.valid());
    }

    void alignmentKeepsSubMillisecondFirePhase()
    {
        const double stamp = alignFireEpochMs(
            100.125, 100.700, qint64{1'700'000'000'000}, 100.900);
        QCOMPARE(fireEpochLogField(stamp),
                 QStringLiteral("fire_epoch_ms=1699999999999.325"));
    }

    void capturedStampTracksWallClockWithinFiveMilliseconds()
    {
        const auto monotonicNowMs = [] {
            return std::chrono::duration<double, std::milli>(
                       std::chrono::steady_clock::now().time_since_epoch())
                .count();
        };

        const double fireMonotonicMs = monotonicNowMs();
        const double fireEpochMs = captureFireEpochMs(
            fireMonotonicMs, monotonicNowMs);
        const double wallAfterMs = static_cast<double>(
            QDateTime::currentMSecsSinceEpoch());

        QVERIFY2(std::isfinite(fireEpochMs), "fire epoch must be finite");
        QVERIFY2(std::abs(wallAfterMs - fireEpochMs) < 5.0,
                 qPrintable(QStringLiteral("fire=%1 wall=%2 delta=%3")
                                .arg(fireEpochMs, 0, 'f', 3)
                                .arg(wallAfterMs, 0, 'f', 3)
                                .arg(wallAfterMs - fireEpochMs, 0, 'f', 3)));
        QVERIFY(fireEpochLogField(fireEpochMs).startsWith(
            QStringLiteral("fire_epoch_ms=")));
    }

    void invalidClockSampleUsesExplicitSentinel()
    {
        QCOMPARE(alignFireEpochMs(10.0, 12.0, 1000, 11.0), -1.0);
        QCOMPARE(fireEpochLogField(-1.0),
                 QStringLiteral("fire_epoch_ms=-1.000"));
    }
};

QTEST_APPLESS_MAIN(FireEpochClockTests)

#include "FireEpochClockTests.moc"
