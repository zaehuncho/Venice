// [CL2-P8-002/006/008 2026-09-23] Contract tests for the licence recovery path
// (docs/redteam/2026-09-22-launch/P8-real-world-robustness.md):
//   * LicenseHeartbeatBackoff  - 15 s / 30 s / 60 s after failures, then 5 min;
//                                success resets; rate_limited jumps to 60 s.
//   * immediateHeartbeatAllowed - resume / network-online debounce.
//   * leaseNoticeKind/Text      - plain copy while the lease blocks shots.
//   * licenseErrorUserText      - timestamp_expired / device_mismatch mapped to
//                                customer text (SecurityCore).
// Pure policy: no sockets, no timers, no clocks.

#include "LeaseGate.h"
#include "LicenseClient.h"
#include "LicenseHeartbeatPolicy.h"
#include "UiNotificationPolicy.h"

#include <QtTest/QTest>

#include <algorithm>
#include <functional>

using namespace orion;

namespace {

// [CL2-P8-002 round 2 2026-09-23] Fake-clock harness. It reproduces exactly what
// OrionAppController does with a HeartbeatAction (repeating QTimer restart/stop,
// LicenseClient::validate no-op while in flight, LeaseGate refreshed ONLY on an
// Ok reply) against a scripted fake server with fixed latency. The controller
// itself is not constructed (it needs the whole GUI/automation stack); every
// decision it makes comes from LicenseHeartbeatCoordinator, which this drives.
struct HeartbeatSim {
    static constexpr qint64 kEpochBaseS = 1'790'000'000;
    static constexpr qint64 kServerLeaseTtlS = 900;

    LicenseHeartbeatCoordinator coordinator;
    LeaseGate gate;
    qint64 nowMs = 0;
    qint64 timerDueMs = -1;
    int timerIntervalMs = 0;
    bool inFlight = false;
    qint64 replyAtMs = -1;
    qint64 latencyMs = 1'000;
    qint64 serverLeaseTtlS = kServerLeaseTtlS;
    int replies = 0;
    QList<qint64> sendsMs;
    std::function<HeartbeatOutcome(int)> server = [](int) { return HeartbeatOutcome::Ok; };

    qint64 epochS() const { return kEpochBaseS + nowMs / 1000; }

    void activate()
    {
        gate.setEnabled(true);
        gate.recordActivation(0, nowMs, epochS());
        apply(coordinator.onSessionStarted());
    }

    void apply(const HeartbeatAction& a)
    {
        if (a.stopTimer) {
            timerDueMs = -1;
        }
        if (a.restartTimerMs >= 0) {
            timerIntervalMs = a.restartTimerMs;
            timerDueMs = nowMs + a.restartTimerMs;
        }
        if (a.sendNow) {
            send();
        }
    }

    void send()
    {
        if (inFlight) {
            return;   // LicenseClient::validate no-op
        }
        inFlight = true;
        sendsMs.append(nowMs);
        replyAtMs = nowMs + latencyMs;
    }

    void event() { apply(coordinator.onImmediateRequest(nowMs, inFlight)); }

    // Suspend: time passes, nothing is processed.
    void sleepUntil(qint64 tMs) { nowMs = tMs; }

    void runUntil(qint64 tMs)
    {
        for (;;) {
            qint64 next = -1;
            if (replyAtMs >= 0) {
                next = replyAtMs;
            }
            if (timerDueMs >= 0 && (next < 0 || timerDueMs < next)) {
                next = timerDueMs;
            }
            if (next < 0 || next > tMs) {
                nowMs = std::max(nowMs, tMs);
                return;
            }
            nowMs = std::max(nowMs, next);   // overdue after a sleep fires at wake time
            if (replyAtMs >= 0 && replyAtMs <= nowMs) {
                inFlight = false;
                replyAtMs = -1;
                const HeartbeatOutcome outcome = server(replies++);
                if (outcome == HeartbeatOutcome::Ok) {
                    gate.recordHeartbeatOk(epochS() + serverLeaseTtlS, nowMs, epochS());
                }
                apply(coordinator.onResult(outcome, gate.fireAllowed(nowMs, epochS())));
            } else {
                timerDueMs = nowMs + timerIntervalMs;   // repeating QTimer
                send();
            }
        }
    }

    bool fireAllowed() const { return gate.fireAllowed(nowMs, epochS()); }
    LeaseNoticeKind notice() const
    {
        return leaseNoticeKind(gate.enabled(), true, fireAllowed(), coordinator.clockOffAtReceipt());
    }
};

} // namespace

class LicenseHeartbeatPolicyTests final : public QObject {
    Q_OBJECT

private slots:
    void backoffLaddersThenFallsBackToNormalCadence()
    {
        LicenseHeartbeatBackoff b;
        QCOMPARE(b.nextDelayMs(), LicenseHeartbeatBackoff::kNormalIntervalMs);
        b.recordFailure();
        QCOMPARE(b.nextDelayMs(), 15'000);
        b.recordFailure();
        QCOMPARE(b.nextDelayMs(), 30'000);
        b.recordFailure();
        QCOMPARE(b.nextDelayMs(), 60'000);
        b.recordFailure();
        QCOMPARE(b.nextDelayMs(), 5 * 60 * 1000);
        for (int i = 0; i < 5000; ++i) {
            b.recordFailure();   // long outage never drops below the normal cadence
        }
        QCOMPARE(b.nextDelayMs(), 5 * 60 * 1000);
    }

    void successResetsTheLadder()
    {
        LicenseHeartbeatBackoff b;
        b.recordFailure();
        b.recordFailure();
        b.recordSuccess();
        QCOMPARE(b.consecutiveFailures(), 0);
        QCOMPARE(b.nextDelayMs(), LicenseHeartbeatBackoff::kNormalIntervalMs);
        b.recordFailure();
        QCOMPARE(b.nextDelayMs(), 15'000);
    }

    void rateLimitedSkipsTheShortRungs()
    {
        LicenseHeartbeatBackoff b;
        b.recordFailure(/*rateLimited=*/true);
        QCOMPARE(b.nextDelayMs(), 60'000);
        b.recordFailure(true);
        QCOMPARE(b.nextDelayMs(), 5 * 60 * 1000);
    }

    // Three failures then recovery: attempts land at ~15, 30, 60 s after each
    // failure -> the fourth (successful) attempt is at most 105 s after the first
    // failure, far inside the 15-min max-staleness window.
    void threeFailuresThenSuccessRecoversWithinTheLadder()
    {
        LicenseHeartbeatBackoff b;
        qint64 t = 0;
        QList<int> delays;
        for (int i = 0; i < 3; ++i) {
            b.recordFailure();
            delays.append(b.nextDelayMs());
            t += b.nextDelayMs();
        }
        QCOMPARE(delays, (QList<int>{15'000, 30'000, 60'000}));
        QCOMPARE(t, qint64(105'000));
        b.recordSuccess();
        QCOMPARE(b.nextDelayMs(), LicenseHeartbeatBackoff::kNormalIntervalMs);
    }

    // Worst case request rate stays far under the backend cap (30 / key / 60 s).
    void ladderNeverHammersTheServer()
    {
        LicenseHeartbeatBackoff b;
        qint64 elapsed = 0;
        int requests = 0;
        while (elapsed < 60'000) {
            b.recordFailure();
            elapsed += b.nextDelayMs();
            ++requests;
        }
        QVERIFY(requests <= 4);
    }

    void immediateHeartbeatIsDebounced()
    {
        QVERIFY(immediateHeartbeatAllowed(-1, 0));
        QVERIFY(immediateHeartbeatAllowed(-1, 123'456));
        QVERIFY(!immediateHeartbeatAllowed(1'000, 1'000));
        QVERIFY(!immediateHeartbeatAllowed(1'000, 1'000 + kImmediateHeartbeatMinSpacingMs - 1));
        QVERIFY(immediateHeartbeatAllowed(1'000, 1'000 + kImmediateHeartbeatMinSpacingMs));
        // Non-monotonic input never wedges recovery.
        QVERIFY(immediateHeartbeatAllowed(50'000, 10'000));
    }

    void leaseNoticeOnlyWhenSignedInAndBlocked()
    {
        // gate, authed, fireAllowed, lastOk
        QVERIFY(leaseNoticeKind(false, true, false, false) == LeaseNoticeKind::None);   // gate off
        QVERIFY(leaseNoticeKind(true, false, false, false) == LeaseNoticeKind::None);   // AuthGate owns it
        QVERIFY(leaseNoticeKind(true, true, true, false) == LeaseNoticeKind::None);     // shots allowed
        QVERIFY(leaseNoticeKind(true, true, true, true) == LeaseNoticeKind::None);
        QVERIFY(leaseNoticeKind(true, true, false, false) == LeaseNoticeKind::Reconnecting);
        QVERIFY(leaseNoticeKind(true, true, false, true) == LeaseNoticeKind::ClockOff);
    }

    void leaseNoticeTextIsPlain()
    {
        QVERIFY(leaseNoticeText(LeaseNoticeKind::None).isEmpty());
        const QString reconnecting = leaseNoticeText(LeaseNoticeKind::Reconnecting);
        QVERIFY(reconnecting.startsWith(QStringLiteral("Reconnecting to Venice servers")));
        QVERIFY(reconnecting.contains(QStringLiteral("shots paused")));
        const QString clock = leaseNoticeText(LeaseNoticeKind::ClockOff);
        QVERIFY(clock.contains(QStringLiteral("PC clock is off")));
        QVERIFY(clock.contains(QStringLiteral("Set time automatically")));
        QVERIFY(!reconnecting.contains(QStringLiteral("lease"), Qt::CaseInsensitive));
        QVERIFY(!clock.contains(QStringLiteral("lease"), Qt::CaseInsensitive));
    }

    // CL2-P8-006: the bare server code must never reach the customer.
    void timestampExpiredMapsToClockCopy()
    {
        LicenseResult r;
        r.error = QStringLiteral("timestamp_expired");
        r.message = QStringLiteral("timestamp_expired");   // what the parser sets with no server message
        const QString text = licenseErrorUserText(r, QStringLiteral("fallback"));
        QVERIFY(text.contains(QStringLiteral("PC clock is off")));
        QVERIFY(text.contains(QStringLiteral("Set time automatically")));
        QVERIFY(!text.contains(QStringLiteral("timestamp_expired")));
    }

    // CL2-P8-008: activation's device_mismatch has no message; heartbeat's has prose.
    // Both show the same plain line.
    void deviceMismatchMapsToRebindCopy()
    {
        LicenseResult r;
        r.error = QStringLiteral("device_mismatch");
        r.message = QStringLiteral("device_mismatch");
        const QString text = licenseErrorUserText(r);
        QVERIFY(text.contains(QStringLiteral("linked to a different PC")));
        QVERIFY(text.contains(QStringLiteral("/hwid_reset")));
        QVERIFY(!text.contains(QStringLiteral("device_mismatch")));
        r.message = QStringLiteral("License is bound to a different machine.");
        QCOMPARE(licenseErrorUserText(r), text);
        // Still a kill code: the mapping changes copy only, never the verdict.
        QVERIFY(isLicenseKillCode(QStringLiteral("device_mismatch")));
        QVERIFY(!isLicenseKillCode(QStringLiteral("timestamp_expired")));
        QVERIFY(!isLicenseKillCode(QStringLiteral("rate_limited")));
        // A secondary purchase lookup outage is retriable; only the already
        // bounded signed lease may keep the session going until it expires.
        QVERIFY(!isLicenseKillCode(QStringLiteral("entitlement_unavailable")));
    }

    // -- [CL2-P8-002 round 2 2026-09-23] fake-clock / fake-server event tests --

    void simRetryLadderThenSuccessResets()
    {
        HeartbeatSim sim;
        sim.server = [](int i) { return i < 3 ? HeartbeatOutcome::Failure : HeartbeatOutcome::Ok; };
        sim.activate();
        sim.runUntil(3'600'000);
        QVERIFY(sim.sendsMs.size() >= 5);
        const qint64 lat = sim.latencyMs;
        QCOMPARE(sim.sendsMs[0], qint64(300'000));   // normal cadence first
        QCOMPARE(sim.sendsMs[1] - (sim.sendsMs[0] + lat), qint64(15'000));
        QCOMPARE(sim.sendsMs[2] - (sim.sendsMs[1] + lat), qint64(30'000));
        QCOMPARE(sim.sendsMs[3] - (sim.sendsMs[2] + lat), qint64(60'000));
        QCOMPARE(sim.sendsMs[4] - (sim.sendsMs[3] + lat), qint64(300'000));   // success reset
        QCOMPARE(sim.coordinator.consecutiveFailures(), 0);
        QVERIFY(sim.coordinator.lastHeartbeatServerOk());
        QVERIFY(sim.fireAllowed());
    }

    void simKillStopsRetries()
    {
        HeartbeatSim sim;
        sim.server = [](int) { return HeartbeatOutcome::Kill; };
        sim.activate();
        sim.runUntil(4 * 3'600'000);
        QCOMPARE(sim.sendsMs.size(), 1);
        QCOMPARE(sim.timerDueMs, qint64(-1));
    }

    void simRateLimitedWaitsSixtySeconds()
    {
        HeartbeatSim sim;
        sim.server = [](int i) { return i == 0 ? HeartbeatOutcome::RateLimited : HeartbeatOutcome::Ok; };
        sim.activate();
        sim.runUntil(400'000);
        QVERIFY(sim.sendsMs.size() >= 2);
        QCOMPARE(sim.sendsMs[1] - (sim.sendsMs[0] + sim.latencyMs), qint64(60'000));
    }

    // Failures only REQUEST a refresh: however many there are, the activation
    // lease still ends on time and the notice says Reconnecting, not Ready.
    void simFailuresNeverExtendTheLease()
    {
        HeartbeatSim sim;
        sim.server = [](int) { return HeartbeatOutcome::Failure; };
        sim.activate();
        sim.runUntil(899'000);
        QVERIFY(sim.fireAllowed());
        QVERIFY(sim.sendsMs.size() >= 4);
        sim.runUntil(900'000);
        QVERIFY(!sim.fireAllowed());
        QVERIFY(sim.notice() == LeaseNoticeKind::Reconnecting);
        sim.runUntil(6 * 3'600'000);
        QVERIFY(!sim.fireAllowed());
        // A long outage settles on the 5-min cadence (~12 requests per hour).
        const qint64 lastHourStart = sim.nowMs - 3'600'000;
        const auto inLastHour = std::count_if(sim.sendsMs.cbegin(), sim.sendsMs.cend(),
                                              [&](qint64 t) { return t >= lastHourStart; });
        QVERIFY(inLastHour <= 13);
    }

    void simPreviouslyValidLeaseAgesOutWithoutClockWarning()
    {
        HeartbeatSim sim;
        sim.activate();
        sim.runUntil(301'000);             // signed, valid heartbeat received
        QVERIFY(sim.coordinator.lastHeartbeatServerOk());
        QVERIFY(sim.fireAllowed());
        sim.sleepUntil(1'300'000);          // both clocks advance during sleep
        QVERIFY(!sim.fireAllowed());
        QVERIFY(sim.notice() == LeaseNoticeKind::Reconnecting);
        sim.event();                        // missed power event fallback: one request
        QCOMPARE(sim.sendsMs.size(), 2);
        sim.event();                        // debounced duplicate
        QCOMPARE(sim.sendsMs.size(), 2);
    }

    void simFreshlyVerifiedAlreadyExpiredLeaseShowsClockWarning()
    {
        HeartbeatSim sim;
        sim.serverLeaseTtlS = -1;
        sim.activate();
        sim.runUntil(301'000);
        QVERIFY(sim.coordinator.lastHeartbeatServerOk());
        QVERIFY(sim.coordinator.clockOffAtReceipt());
        QVERIFY(!sim.fireAllowed());
        QVERIFY(sim.notice() == LeaseNoticeKind::ClockOff);
    }

    // Codex round 1 race: resume lands while a stale request is in flight.
    void simEventDuringInFlightRerunsOnceAfterFailure()
    {
        HeartbeatSim sim;
        sim.latencyMs = 10'000;
        sim.server = [](int i) { return i == 0 ? HeartbeatOutcome::Failure : HeartbeatOutcome::Ok; };
        sim.activate();
        sim.sleepUntil(1'000'000);          // slept past the 900 s lease
        sim.runUntil(1'000'000);            // overdue 5-min tick fires at wake -> in flight
        QCOMPARE(sim.sendsMs.size(), 1);
        QVERIFY(sim.inFlight);
        sim.runUntil(1'002'000);
        sim.event();                        // WM_POWERBROADCAST resume
        QVERIFY(sim.coordinator.rerunPending());
        QCOMPARE(sim.sendsMs.size(), 1);    // validate would have no-op'd
        sim.runUntil(1'005'000);
        sim.event();                        // second resume code: debounced, still one re-run
        QVERIFY(!sim.fireAllowed());
        QVERIFY(sim.notice() == LeaseNoticeKind::Reconnecting);   // truthful while unconfirmed
        sim.runUntil(1'010'000);            // stale request fails -> re-run immediately
        QCOMPARE(sim.sendsMs.size(), 2);
        QCOMPARE(sim.sendsMs[1], qint64(1'010'000));
        QCOMPARE(sim.coordinator.consecutiveFailures(), 0);   // stale failure not counted
        QVERIFY(!sim.coordinator.rerunPending());
        QVERIFY(sim.notice() == LeaseNoticeKind::Reconnecting);
        sim.runUntil(1'020'000);            // re-run succeeds
        QVERIFY(sim.fireAllowed());
        QVERIFY(sim.notice() == LeaseNoticeKind::None);
        sim.runUntil(1'300'000);
        QCOMPARE(sim.sendsMs.size(), 2);    // bounded: no extra request, back to 5 min
    }

    void simEventDuringInFlightSuccessDoesNotRerun()
    {
        HeartbeatSim sim;
        sim.latencyMs = 10'000;
        sim.activate();
        sim.runUntil(302'000);
        sim.event();
        QVERIFY(sim.coordinator.rerunPending());
        sim.runUntil(310'000);
        QVERIFY(!sim.coordinator.rerunPending());
        sim.runUntil(600'000);
        QCOMPARE(sim.sendsMs.size(), 1);
    }

    void simEventDuringInFlightRateLimitedDoesNotRerun()
    {
        HeartbeatSim sim;
        sim.latencyMs = 10'000;
        sim.server = [](int i) { return i == 0 ? HeartbeatOutcome::RateLimited : HeartbeatOutcome::Ok; };
        sim.activate();
        sim.runUntil(302'000);
        sim.event();
        sim.runUntil(310'000);
        QCOMPARE(sim.sendsMs.size(), 1);
        sim.runUntil(371'000);
        QCOMPARE(sim.sendsMs.size(), 2);
        QCOMPARE(sim.sendsMs[1], qint64(370'000));   // 60 s after the 429
    }

    void simIdleEventSendsImmediatelyAndDebounces()
    {
        HeartbeatSim sim;
        sim.activate();
        sim.runUntil(50'000);
        sim.event();
        QCOMPARE(sim.sendsMs.size(), 1);
        QCOMPARE(sim.sendsMs[0], qint64(50'000));
        sim.runUntil(55'000);
        sim.event();                        // < 10 s: debounced
        QCOMPARE(sim.sendsMs.size(), 1);
    }

    // Round 2 item 4: retry chatter stays out of the customer Activity ring; the
    // banner transition lines stay in it.
    void retryLinesAreEngineeringOnly()
    {
        using ui_notifications::shouldEnterActivityRing;
        QVERIFY(!shouldEnterActivityRing(heartbeatRetryLogLine(QString(), 15'000, QStringLiteral("Lease stale"))));
        QVERIFY(!shouldEnterActivityRing(heartbeatRetryLogLine(QStringLiteral("rate_limited"), 60'000,
                                                               QStringLiteral("Lease valid (120 s left)"))));
        QVERIFY(!shouldEnterActivityRing(heartbeatImmediateLogLine(QStringLiteral("resumed from sleep"), true,
                                                                   QStringLiteral("Lease expired"))));
        QVERIFY(!shouldEnterActivityRing(heartbeatImmediateLogLine(QStringLiteral("network back online"), false,
                                                                   QStringLiteral("No lease"))));
        QVERIFY(!shouldEnterActivityRing(heartbeatRecoveredLogLine(3, QStringLiteral("Lease valid (900 s left)"))));
        QVERIFY(!shouldEnterActivityRing(QStringLiteral(
            "Lease heartbeat: server code device_mismatch shown to the customer as: x")));
        QVERIFY(shouldEnterActivityRing(leaseNoticeText(LeaseNoticeKind::Reconnecting)));
        QVERIFY(shouldEnterActivityRing(leaseNoticeText(LeaseNoticeKind::ClockOff)));
        QVERIFY(shouldEnterActivityRing(leaseRestoredLogLine()));
        // The pre-existing customer kill line is unchanged.
        QVERIFY(shouldEnterActivityRing(QStringLiteral("License heartbeat: session disabled (revoked)")));
    }
};

QTEST_APPLESS_MAIN(LicenseHeartbeatPolicyTests)

#include "LicenseHeartbeatPolicyTests.moc"
