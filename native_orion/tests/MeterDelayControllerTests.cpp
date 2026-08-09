#include "MeterDelayController.h"
// Header-only (Qt6::Core): the syncEngineArmed() disarm-cause split asserted by
// midLockTargetChangeIsADelayGateDisarmOnly().
#include "ControllerRoutingPolicy.h"

#include <QtCore/QList>
#include <QtTest/QSignalSpy>
#include <QtTest/QTest>

#include <cmath>
#include <memory>

using orion::MeterDelayController;
using SessionState = MeterDelayController::SessionState;
using ShotState = MeterDelayController::ShotState;

namespace {

// Deterministic harness: manual ticks + an injected monotonic clock so ramp
// rates and dwell windows can be asserted exactly instead of raced against a
// real 50 ms QTimer.
class Harness {
public:
    Harness()
    {
        controller_ = std::make_unique<MeterDelayController>();
        // Every test below this point describes SHOT-GATED behaviour: the raw
        // physical Square edge, the per-shot ramp, the engagement dwell and its
        // cancellation. That is no longer the default policy (AlwaysOn is), so
        // it must be selected explicitly. The policies themselves are covered
        // separately in the engagement-policy section at the end of the file.
        controller_->setEngagePolicy(MeterDelayController::EngagePolicy::ShotGated);
        controller_->setManualTickMode(true);
        controller_->setMonotonicClockForTesting([this]() { return now_; });
        QObject::connect(controller_.get(), &MeterDelayController::delayCommanded,
                         controller_.get(),
                         [this](double ms) { commands_.append(ms); });
    }

    MeterDelayController& c() { return *controller_; }
    MeterDelayController* ptr() { return controller_.get(); }
    QList<double>& commands() { return commands_; }
    qint64 now() const { return now_; }

    void advance(qint64 ms) { now_ += ms; }

    void tick(int count = 1)
    {
        for (int i = 0; i < count; ++i) {
            now_ += MeterDelayController::kTickMs;
            controller_->tick();
        }
    }

    // Drive the session layer all the way to Ready with a clean baseline
    // (jitter 2 ms, rtt 20 ms).
    bool arm()
    {
        controller_->setEnabled(true);
        controller_->setPlayingGame(true);
        controller_->setCourtIpKnown(true);
        controller_->tick(); // Idle -> Probing
        if (controller_->sessionState() != SessionState::Probing) return false;
        for (int i = 0; i < 10; ++i) {
            controller_->updateNetworkHealth(20.0, 2.0, true, false);
            now_ += 300;
            controller_->tick();
        }
        return controller_->sessionState() == SessionState::Ready;
    }

    // Number of ticks a full 0 -> target ramp needs, derived rather than
    // hardcoded. This was 10, which was enough at the old 55ms-per-tick slew and
    // is nowhere near enough at the safe 5ms-per-tick one. Deriving it means a
    // future slew change moves the budget automatically instead of turning every
    // test in the file red.
    static int ticksToFullRamp()
    {
        return static_cast<int>(
                   std::ceil(MeterDelayController::kHardMaxMs
                             / MeterDelayController::kRampUpPerTickMs))
            + 4;
    }

    // Engage and hold until the shot layer reaches Locked.
    //
    // NOTE this deliberately keeps the physical button HELD for the whole ramp.
    // At the safe slew the ramp (~1650ms) is longer than kDefaultMinEngagedMs
    // (900ms), so a released button would start disengaging before Locked is
    // ever reached -- see shotGatedCannotSettleBeforeTheMeterRenders().
    bool lockShot()
    {
        controller_->setPhysicalSquareHeld(true);
        for (int i = 0; i < ticksToFullRamp()
                 && controller_->shotState() != ShotState::Locked; ++i) {
            tick();
        }
        return controller_->shotState() == ShotState::Locked;
    }

    void destroyController() { controller_.reset(); }

private:
    qint64 now_ = 1'000'000;
    // MUST be declared before controller_: ~MeterDelayController commands 0 ms,
    // which fires delayCommanded into this list. Reverse-order destruction would
    // otherwise append to an already-destroyed QList.
    QList<double> commands_;
    std::unique_ptr<MeterDelayController> controller_;
};

double expectedKey(double delayMs)
{
    return std::round(delayMs / MeterDelayController::kConditionQuantumMs)
        * MeterDelayController::kConditionQuantumMs;
}

} // namespace

class MeterDelayControllerTests final : public QObject {
    Q_OBJECT

private slots:
    // [ORION_METER_DELAY_FENCE] This suite exercises the controller's ARMING and RAMPING
    // behaviour, which a production build deliberately makes impossible: MeterDelayController
    // is production-fenced (MeterDelayController.cpp:48-61) and refuses to enable at all,
    // entering Idle with "The inbound meter delay cannot be enabled in a shipping build."
    // Every arm() here therefore returns false under ORION_PRODUCTION_BUILD — 25 of 30 tests
    // failed for that single reason, which is what has been blocking the production gate.
    //
    // Production behaviour is NOT left uncovered: OrionMeterDelayFenceProdTests is compiled
    // WITH ORION_PRODUCTION_BUILD (CMakeLists.txt:625) specifically to assert the fence holds.
    // This suite stays fully live in dev builds, where the behaviour it tests is reachable.
    void initTestCase();
    void sessionArmsOnlyAfterProbeSamplesAndTime();
    void rampUpRateAndLockTiming();
    void rampDownRate();
    void rapidSuccessionResumesFromPartialRamp();
    void shotCancelledMidRamp();
    void shortTapHoldsDelayThroughMeterWindow();
    void botHintCannotStartEngagement();
    void backoffLowersTargetWhileShotLayerTracks();
    void manualOverrideClamping();
    void delayForcedToZeroOnDisable();
    void delayForcedToZeroOnSessionIdle();
    void delayForcedToZeroOnStreamEnd();
    void delayForcedToZeroOnDestruction();
    void shotInputIgnoredWhileSessionIdle();
    void conditionEpochOnlyChangesWithSettledTarget();
    void engagementWatchdogReleasesStuckHint();
    void lockedDwellKeepsReassertingTheDelay();
    void zeroDelayIsNotKeptAlive();
    // Engagement policy
    void defaultPolicyIsAlwaysOn();
    void controllerSlewMatchesTheServiceCap();
    void alwaysOnEngagesWithNoShotInputAndNeverReleases();
    void alwaysOnHoldsOneConstantDelayAcrossManyShots();
    void deadBallWaitsForADeadBallThenHoldsLikeAlwaysOn();
    // [ORION_DEFENSE_FLAG 2026-08-08] OffenseDefense is now gated SOLELY by the
    // manual D-pad Up defense flag; the shot-cycle/square/edge-latch wiring and
    // the offense_ possession input are out of the decision. These pin the new
    // contract (names per the owner brief).
    void dpadUpToggleDrivesDefenseFlagOnly();
    void defenseFlagBypassesDelay();
    void shotCycleNoLongerDrivesBypass();
    void defenseFlagResetsOnSessionRestart();
    void defenseReengageIsUnsettledDuringRamp();
    void offenseHeldPastWatchdogNeverDisarmsTheEngine();
    void shotGatedReportsThatItCannotSettleInTime();
    void policyChangeCannotStrandAHeldDelay();
    void deadBallLatchDoesNotSurviveTheSession();
    void midLockTargetChangeIsADelayGateDisarmOnly();
};

// nexus_svc forces the delay to 0 when set_meter_delay stops arriving for
// _METER_WATCHDOG_TIMEOUT_S. A shot holds a CONSTANT delay for the whole
// kDefaultMinEngagedMs dwell, so change-only emission starved that watchdog and
// the service zeroed the delay mid-meter-read.
static constexpr qint64 kServiceWatchdogMs = 500;

void MeterDelayControllerTests::lockedDwellKeepsReassertingTheDelay()
{
    Harness h;
    QVERIFY(h.arm());
    QVERIFY(h.lockShot());
    const double target = h.c().targetDelayMs();

    const int before = h.commands().size();
    const qint64 startMs = h.now();
    // Longest engagement the shot layer can sustain without a physical hold.
    QList<qint64> gaps;
    qint64 lastEmitMs = startMs;
    int seen = before;
    while (h.now() - startMs < MeterDelayController::kDefaultMinEngagedMs + 200) {
        h.tick();
        if (h.commands().size() != seen) {
            seen = h.commands().size();
            gaps.append(h.now() - lastEmitMs);
            lastEmitMs = h.now();
        }
    }
    gaps.append(h.now() - lastEmitMs);

    QVERIFY2(h.commands().size() > before,
             "no set_meter_delay was re-asserted during the Locked dwell — the "
             "service watchdog would zero the delay mid-meter-read");
    for (qint64 gap : gaps) {
        QVERIFY2(gap < kServiceWatchdogMs,
                 qPrintable(QStringLiteral("%1 ms of silence exceeds the %2 ms "
                                           "service watchdog")
                                .arg(gap).arg(kServiceWatchdogMs)));
    }
    // Every keepalive carries the same value; the delay itself never moves.
    for (int i = before; i < h.commands().size(); ++i) {
        QCOMPARE(h.commands().at(i), target);
    }
    QCOMPARE(h.c().currentDelayMs(), target);
}

void MeterDelayControllerTests::zeroDelayIsNotKeptAlive()
{
    // A zero delay needs no keepalive: the service watchdog ignores it, and
    // spamming set_meter_delay(0) between shots would be pure noise.
    Harness h;
    QVERIFY(h.arm());
    h.tick(40);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    QVERIFY2(h.commands().isEmpty(),
             "an idle controller must not emit any delay command");
}

void MeterDelayControllerTests::initTestCase()
{
    // Production fence was retired 2026-08-07 (meter delay ships as an opt-in
    // customer feature). The previous QSKIP under ORION_PRODUCTION_BUILD is
    // therefore removed; every test in this suite now runs in every build.
}

void MeterDelayControllerTests::sessionArmsOnlyAfterProbeSamplesAndTime()
{
    Harness h;
    QSignalSpy startSpy(h.ptr(), &MeterDelayController::interceptStartRequested);
    QSignalSpy stopSpy(h.ptr(), &MeterDelayController::interceptStopRequested);

    h.c().setEnabled(true);
    h.c().setPlayingGame(true);
    h.c().setCourtIpKnown(true);
    QCOMPARE(h.c().sessionState(), SessionState::Idle);

    h.c().tick();
    QCOMPARE(h.c().sessionState(), SessionState::Probing);
    QCOMPARE(startSpy.count(), 1);

    // Eight samples but only ~80 ms elapsed: the minimum probe window is not met.
    for (int i = 0; i < 8; ++i) {
        h.c().updateNetworkHealth(20.0, 2.0, true, false);
        h.advance(10);
        h.c().tick();
    }
    QCOMPARE(h.c().sessionState(), SessionState::Probing);

    h.advance(MeterDelayController::kMinProbeMs + 50);
    h.c().tick();
    QCOMPARE(h.c().sessionState(), SessionState::Ready);
    // Verified 20 ms RTT probes -> one-way 10 ms -> the adaptive target
    // (kDesiredTotalInboundMs - 10) clamps to the ceiling.
    QCOMPARE(h.c().targetDelayMs(), MeterDelayController::kAdaptiveCeilingMs);
    QCOMPARE(stopSpy.count(), 0);

    // Game end tears the intercept down.
    h.c().setPlayingGame(false);
    QCOMPARE(h.c().sessionState(), SessionState::Idle);
    QCOMPARE(stopSpy.count(), 1);
}

void MeterDelayControllerTests::rampUpRateAndLockTiming()
{
    Harness h;
    QVERIFY(h.arm());
    // Auto mode adapts the target to the measured RTT inside the
    // [kAdaptiveFloorMs, kAdaptiveCeilingMs] band, so assert against the live
    // target rather than the nominal default.
    const double target = h.c().targetDelayMs();
    QVERIFY(target >= MeterDelayController::kAdaptiveFloorMs
            && target <= MeterDelayController::kAdaptiveCeilingMs);

    const qint64 edgeMs = h.now();
    // The gate is the RAW physical square edge, and it takes effect
    // synchronously rather than waiting for the next 50 ms tick.
    h.c().setPhysicalSquareHeld(true);
    QCOMPARE(h.c().shotState(), ShotState::Engaging);
    QCOMPARE(h.c().currentDelayMs(), MeterDelayController::kRampUpPerTickMs);

    double previous = h.c().currentDelayMs();
    while (h.c().shotState() != ShotState::Locked && h.now() - edgeMs < 5000) {
        h.tick();
        const double next = h.c().currentDelayMs();
        QVERIFY2(next - previous <= MeterDelayController::kRampUpPerTickMs + 1e-6,
                 "ramp-up exceeded the per-tick slew cap");
        previous = next;
    }

    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), target);
    QVERIFY(h.c().conditionSettled());

    // ── The crux, stated as an assertion ────────────────────────────────────
    // This used to require lockLatency <= 200ms, "fully settled well before the
    // meter renders at ~300 ms". That requirement is UNSATISFIABLE at a slew
    // that does not stutter the game: reaching ~150ms at 100 ms/s takes ~1.5s,
    // and buying the old 200ms would mean D' = +0.75, i.e. the console dropping
    // to 57% of its normal update rate right before the read. You cannot have
    // both a per-shot ramp and a non-stuttering one. The delay must therefore be
    // established long before the shot -- which is what every other engagement
    // policy does, and why AlwaysOn is the default.
    const qint64 lockLatency = h.now() - edgeMs;
    const double predictedMs = (target / MeterDelayController::kSlewMsPerSec) * 1000.0;
    QVERIFY2(std::abs(static_cast<double>(lockLatency) - predictedMs) <= 2.0 * MeterDelayController::kTickMs,
             qPrintable(QStringLiteral("locked after %1 ms, predicted %2 ms")
                            .arg(lockLatency).arg(predictedMs)));
    QVERIFY2(lockLatency > MeterDelayController::kPreMeterBudgetMs,
             "a per-shot ramp that beats the meter is exactly the stutter that "
             "got this feature deleted");
    QVERIFY(!h.c().policyCanSettleBeforeMeter());
}

void MeterDelayControllerTests::rampDownRate()
{
    Harness h;
    QVERIFY(h.arm());
    h.c().setMinEngagedMs(0);
    h.c().setDisengageHoldoffMs(0);
    QVERIFY(h.lockShot());
    // Captured while still Locked: the ramp-down duration is predicted from the
    // height it starts at, and currentDelayMs() is on its way to 0 by the time
    // the assertion runs.
    const double lockedDelay = h.c().currentDelayMs();

    h.c().setPhysicalSquareHeld(false);
    const qint64 releaseMs = h.now();

    double previous = h.c().currentDelayMs();
    while (h.c().shotState() != ShotState::Standby && h.now() - releaseMs < 2000) {
        h.tick();
        const double next = h.c().currentDelayMs();
        QVERIFY2(previous - next <= MeterDelayController::kRampDownPerTickMs + 1e-6,
                 "ramp-down exceeded the per-tick slew cap");
        previous = next;
    }

    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    // Ramp-down is now SYMMETRIC with ramp-up (both 5ms/tick == 100 ms/s). It
    // used to be 20ms/tick, i.e. D' = -0.40, i.e. a 167% catch-up burst: the
    // console being handed 1.67x its normal update rate for ~400ms. A burst
    // stutters exactly as badly as a starve, so the limit has to apply in both
    // directions. The old 400-600ms window encoded the asymmetry and is gone.
    const qint64 downLatency = h.now() - releaseMs;
    const double predictedDownMs =
        (lockedDelay / MeterDelayController::kSlewMsPerSec) * 1000.0;
    QVERIFY2(std::abs(static_cast<double>(downLatency) - predictedDownMs)
                 <= 2.0 * MeterDelayController::kTickMs,
             qPrintable(QStringLiteral("ramp-down took %1 ms, predicted %2 ms")
                            .arg(downLatency).arg(predictedDownMs)));
    QCOMPARE(h.commands().last(), 0.0);
}

void MeterDelayControllerTests::rapidSuccessionResumesFromPartialRamp()
{
    Harness h;
    QVERIFY(h.arm());
    h.c().setMinEngagedMs(0);
    h.c().setDisengageHoldoffMs(0);
    QVERIFY(h.lockShot());
    const double target = h.c().targetDelayMs();

    h.c().setPhysicalSquareHeld(false);
    h.tick(4); // one tick to enter Disengaging, three ramp-down steps
    QCOMPARE(h.c().shotState(), ShotState::Disengaging);
    const double partial = h.c().currentDelayMs();
    QVERIFY2(partial > 0.0 && partial < target,
             qPrintable(QStringLiteral("partial ramp was %1").arg(partial)));

    h.c().setPhysicalSquareHeld(true);
    QCOMPARE(h.c().shotState(), ShotState::Engaging);
    // Resumes from where it was, never restarting from zero.
    QVERIFY2(h.c().currentDelayMs() >= partial,
             "re-engage must not reset the ramp to zero");
    h.tick(2);
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), target);
}

void MeterDelayControllerTests::shotCancelledMidRamp()
{
    Harness h;
    QVERIFY(h.arm());
    h.c().setMinEngagedMs(0);
    h.c().setDisengageHoldoffMs(0);

    h.c().setPhysicalSquareHeld(true);
    h.tick(); // 55 -> 110
    const double peak = h.c().currentDelayMs();
    QCOMPARE(h.c().shotState(), ShotState::Engaging);
    QVERIFY(peak < MeterDelayController::kDefaultDelayMs);

    h.c().setPhysicalSquareHeld(false);
    h.tick();
    QCOMPARE(h.c().shotState(), ShotState::Disengaging);
    // Cancelling must stop the ramp immediately, not finish it first.
    QVERIFY(h.c().currentDelayMs() <= peak + 1e-6);
    h.tick();
    QVERIFY(h.c().currentDelayMs() < peak);

    for (int i = 0; i < 20 && h.c().shotState() != ShotState::Standby; ++i) {
        h.tick();
    }
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
}

void MeterDelayControllerTests::shortTapHoldsDelayThroughMeterWindow()
{
    // The physical square may be released long before the bot fires. Ramping
    // down at that point would change meter velocity mid-read, so the default
    // engagement dwell must keep the delay pinned.
    Harness h;
    QVERIFY(h.arm());
    const double target = h.c().targetDelayMs();

    h.c().setPhysicalSquareHeld(true);
    h.advance(30);
    h.c().setPhysicalSquareHeld(false);

    h.tick(12); // 600 ms after the tap, still inside the 900 ms dwell
    // The dwell still does its job -- the delay is held, not ramped back down
    // mid-read, which is the property this test was written to protect.
    QVERIFY2(h.c().shotState() == ShotState::Engaging
                 || h.c().shotState() == ShotState::Locked,
             "the engagement dwell must not release during the meter window");
    QVERIFY(h.c().currentDelayMs() > 0.0);

    // But at the safe slew it has NOT reached target, and it never will within
    // the dwell: the ramp needs ~1650ms and kDefaultMinEngagedMs is 900ms. So a
    // shot-gated engagement reads the meter at a partial, shot-dependent delay
    // -- the lead-poisoning failure described in the EngagePolicy notes. This
    // asserts the defect exists rather than pretending the policy works.
    QVERIFY2(h.c().currentDelayMs() < target,
             "shot-gated ramp unexpectedly reached target inside the dwell");
    QVERIFY(!h.c().conditionSettled());

    // ...and it does eventually release.
    for (int i = 0; i < 200 && h.c().shotState() != ShotState::Standby; ++i) {
        h.tick();
    }
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
}

void MeterDelayControllerTests::botHintCannotStartEngagement()
{
    // AutomationEngine's shot state lands ~428 ms after the physical edge, so it
    // was never allowed to START an engagement. [ORION_DEFENSE_FLAG 2026-08-08]
    // Since the manual-defense rewiring it cannot EXTEND one either — the hint
    // is recorded diagnostic state with zero engagement influence under every
    // policy (see shotCycleNoLongerDrivesBypass for the OffenseDefense side).
    Harness h;
    QVERIFY(h.arm());

    h.c().setShotCycleActive(true);
    h.tick(5);
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);

    // A real physical edge engages; with the dwell and holdoff zeroed and the
    // button released, a still-active hint must NOT keep the engagement alive.
    h.c().setMinEngagedMs(0);
    h.c().setDisengageHoldoffMs(0);
    h.c().setPhysicalSquareHeld(true);
    QCOMPARE(h.c().shotState(), ShotState::Engaging);
    h.c().setPhysicalSquareHeld(false);
    h.tick(2);
    QVERIFY2(h.c().shotState() == ShotState::Disengaging
                 || h.c().shotState() == ShotState::Standby,
             "a stuck bot hint kept the engagement alive after the physical "
             "hold ended — the hint must have zero engagement influence");
    for (int i = 0; i < 20 && h.c().shotState() != ShotState::Standby; ++i) {
        h.tick();
    }
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
}

void MeterDelayControllerTests::backoffLowersTargetWhileShotLayerTracks()
{
    Harness h;
    QVERIFY(h.arm());
    QVERIFY(h.lockShot());
    const double target = h.c().targetDelayMs();
    QCOMPARE(h.c().currentDelayMs(), target);

    // Jitter well past 3x the 2 ms baseline.
    h.c().updateNetworkHealth(20.0, 25.0, true, false);
    h.tick();

    QCOMPARE(h.c().sessionState(), SessionState::Backoff);
    QCOMPARE(h.c().targetDelayMs(), target - MeterDelayController::kBackoffStepMs);
    // The shot layer stays locked but tracks the reduced target immediately.
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), h.c().targetDelayMs());

    // Recovery restores the target after the stable window.
    for (int i = 0; i < 60 && h.c().sessionState() != SessionState::Ready; ++i) {
        h.c().updateNetworkHealth(20.0, 2.0, true, false);
        h.tick();
    }
    QCOMPARE(h.c().sessionState(), SessionState::Ready);
    // Recovery recomputes the adaptive target; clean 20 ms RTT clamps to the
    // ceiling.
    QCOMPARE(h.c().targetDelayMs(), MeterDelayController::kAdaptiveCeilingMs);
}

void MeterDelayControllerTests::manualOverrideClamping()
{
    Harness h;
    h.c().setManualDelayMs(9000.0);
    QCOMPARE(h.c().manualDelayMs(), MeterDelayController::kManualMaxMs);
    QVERIFY(h.c().manualMode());

    h.c().setManualDelayMs(5.0);
    QCOMPARE(h.c().manualDelayMs(), MeterDelayController::kManualMinMs);

    h.c().setManualDelayMs(std::nan(""));
    QCOMPARE(h.c().manualDelayMs(), 0.0);
    QVERIFY(!h.c().manualMode());

    h.c().setManualDelayMs(210.0);
    QVERIFY(h.arm());
    QCOMPARE(h.c().targetDelayMs(), 210.0);
    QVERIFY(h.lockShot());
    QCOMPARE(h.c().currentDelayMs(), 210.0);

    // Manual mode disables micro-adjustment drift.
    h.c().setPhysicalSquareHeld(false);
    for (int i = 0; i < 40; ++i) {
        h.c().updateNetworkHealth(20.0, 0.1, true, false);
        h.tick();
    }
    QCOMPARE(h.c().targetDelayMs(), 210.0);
    // Nothing above the hard ceiling was ever commanded.
    for (double command : h.commands()) {
        QVERIFY(command <= MeterDelayController::kHardMaxMs + 1e-6);
        QVERIFY(command >= 0.0);
    }
}

void MeterDelayControllerTests::delayForcedToZeroOnDisable()
{
    Harness h;
    QVERIFY(h.arm());
    QVERIFY(h.lockShot());
    QSignalSpy stopSpy(h.ptr(), &MeterDelayController::interceptStopRequested);

    h.c().setEnabled(false);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    QCOMPARE(h.c().sessionState(), SessionState::Idle);
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.commands().last(), 0.0);
    QCOMPARE(stopSpy.count(), 1);

    // Even a physical edge cannot re-arm a disabled controller.
    h.c().setPhysicalSquareHeld(false);
    h.c().setPhysicalSquareHeld(true);
    h.tick(5);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
}

void MeterDelayControllerTests::delayForcedToZeroOnSessionIdle()
{
    Harness h;
    QVERIFY(h.arm());
    QVERIFY(h.lockShot());

    h.c().setCourtIpKnown(false);
    QCOMPARE(h.c().sessionState(), SessionState::Idle);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    QCOMPARE(h.commands().last(), 0.0);
}

void MeterDelayControllerTests::delayForcedToZeroOnStreamEnd()
{
    Harness h;
    QVERIFY(h.arm());
    QVERIFY(h.lockShot());
    QSignalSpy stopSpy(h.ptr(), &MeterDelayController::interceptStopRequested);

    h.c().notifyStreamEnded();
    QCOMPARE(h.c().sessionState(), SessionState::Idle);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    QCOMPARE(h.commands().last(), 0.0);
    QCOMPARE(stopSpy.count(), 1);
}

void MeterDelayControllerTests::delayForcedToZeroOnDestruction()
{
    Harness h;
    QVERIFY(h.arm());
    QVERIFY(h.lockShot());
    QCOMPARE(h.commands().last(), h.c().targetDelayMs());

    int stopCount = 0;
    QObject::connect(h.ptr(), &MeterDelayController::interceptStopRequested,
                     h.ptr(), [&stopCount]() { ++stopCount; });

    h.destroyController();
    QCOMPARE(h.commands().last(), 0.0);
    QCOMPARE(stopCount, 1);
}

void MeterDelayControllerTests::shotInputIgnoredWhileSessionIdle()
{
    Harness h;
    h.c().setEnabled(true); // but no game / no court IP
    h.tick(3);
    QCOMPARE(h.c().sessionState(), SessionState::Idle);

    h.c().setPhysicalSquareHeld(true);
    h.c().notifyPhysicalShotEdge();
    h.tick(6);
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    QVERIFY(h.commands().isEmpty());
}

void MeterDelayControllerTests::conditionEpochOnlyChangesWithSettledTarget()
{
    Harness h;
    QVERIFY(h.arm());
    h.c().setMinEngagedMs(0);
    h.c().setDisengageHoldoffMs(0);

    const quint64 baseEpoch = h.c().conditionEpoch();
    QVERIFY(h.c().conditionSettled());
    QCOMPARE(h.c().conditionKeyMs(), 0.0);

    QSignalSpy conditionSpy(h.ptr(), &MeterDelayController::delayConditionChanged);

    h.c().setPhysicalSquareHeld(true);
    // Mid-ramp the condition is explicitly unsettled and the epoch is unchanged.
    QVERIFY(!h.c().conditionSettled());
    QCOMPARE(h.c().conditionEpoch(), baseEpoch);

    QVERIFY(h.lockShot());
    QVERIFY(h.c().conditionSettled());
    QCOMPARE(h.c().conditionKeyMs(), expectedKey(h.c().targetDelayMs()));
    const quint64 lockedEpoch = h.c().conditionEpoch();
    QCOMPARE(lockedEpoch, baseEpoch + 1);

    // Holding at the same settled value must not churn the epoch.
    h.tick(10);
    QCOMPARE(h.c().conditionEpoch(), lockedEpoch);
    QCOMPARE(h.c().conditionKeyMs(), expectedKey(h.c().targetDelayMs()));

    h.c().setPhysicalSquareHeld(false);
    for (int i = 0; i < Harness::ticksToFullRamp()
             && h.c().shotState() != ShotState::Standby; ++i) {
        h.tick();
    }
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QVERIFY(h.c().conditionSettled());
    QCOMPARE(h.c().conditionKeyMs(), 0.0);
    QCOMPARE(h.c().conditionEpoch(), lockedEpoch + 1);

    // The epoch is monotonic and every reported epoch is non-decreasing.
    quint64 previous = 0;
    QVERIFY(conditionSpy.count() > 0);
    for (const QList<QVariant>& args : conditionSpy) {
        const quint64 epoch = args.at(0).toULongLong();
        QVERIFY(epoch >= previous);
        previous = epoch;
    }
}

void MeterDelayControllerTests::engagementWatchdogReleasesStuckHint()
{
    Harness h;
    QVERIFY(h.arm());
    h.c().setPhysicalSquareHeld(true);
    h.c().setShotCycleActive(true);
    QVERIFY(h.lockShot());

    // Square released, but the bot hint never clears. The delay must still come
    // back to zero rather than being pinned forever. [ORION_DEFENSE_FLAG
    // 2026-08-08] Post-decoupling the stuck hint has no engagement influence at
    // all, so release now happens at the ordinary dwell/holdoff rather than
    // needing the kMaxEngagedMs watchdog — either way, never pinned.
    h.c().setPhysicalSquareHeld(false);
    for (int i = 0; i < 400 && h.c().shotState() != ShotState::Standby; ++i) {
        h.tick();
    }
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
}


// ─────────────────────────────────────────────────────────────────────────────
//  Engagement policy
// ─────────────────────────────────────────────────────────────────────────────
// The Harness above forces ShotGated because every legacy test describes that
// policy. These build their own controllers so they see the real defaults.

namespace {

// Same deterministic rig, but without the ShotGated override.
class PolicyHarness {
public:
    explicit PolicyHarness(MeterDelayController::EngagePolicy policy)
    {
        controller_ = std::make_unique<MeterDelayController>();
        if (policy != controller_->engagePolicy()) {
            controller_->setEngagePolicy(policy);
        }
        controller_->setManualTickMode(true);
        controller_->setMonotonicClockForTesting([this]() { return now_; });
    }

    MeterDelayController& c() { return *controller_; }
    qint64 now() const { return now_; }
    void advance(qint64 ms) { now_ += ms; }

    void tick(int count = 1)
    {
        for (int i = 0; i < count; ++i) {
            now_ += MeterDelayController::kTickMs;
            controller_->tick();
        }
    }

    bool arm()
    {
        controller_->setEnabled(true);
        controller_->setPlayingGame(true);
        controller_->setCourtIpKnown(true);
        controller_->tick();
        if (controller_->sessionState() != SessionState::Probing) return false;
        for (int i = 0; i < 10; ++i) {
            controller_->updateNetworkHealth(20.0, 2.0, true, false);
            now_ += 300;
            controller_->tick();
        }
        return controller_->sessionState() == SessionState::Ready;
    }

    // Run the ramp out to wherever it settles.
    //
    // MUST tick before testing conditionSettled(): a Standby controller sitting
    // at zero already reports settled, so a plain while(!settled) loop exits
    // immediately and never gives the policy a chance to engage. Tick a few
    // times unconditionally, then run until the ramp actually converges.
    void settle(int maxTicks = 400)
    {
        for (int i = 0; i < maxTicks; ++i) {
            tick();
            if (i >= 3 && controller_->conditionSettled()) break;
        }
    }

private:
    qint64 now_ = 1'000'000;
    std::unique_ptr<MeterDelayController> controller_;
};

} // namespace

void MeterDelayControllerTests::defaultPolicyIsAlwaysOn()
{
    MeterDelayController fresh;
    QCOMPARE(fresh.engagePolicy(), MeterDelayController::EngagePolicy::AlwaysOn);
    QVERIFY(fresh.policyCanSettleBeforeMeter());
}

void MeterDelayControllerTests::controllerSlewMatchesTheServiceCap()
{
    // nexus_svc._METER_MAX_SLEW_MS_PER_S. If these drift apart the service
    // silently clamps the controller and the commanded/applied values diverge,
    // which shows up as a mysteriously slow ramp with no error anywhere.
    QCOMPARE(MeterDelayController::kSlewMsPerSec, 100.0);
    QVERIFY(MeterDelayController::kSlewMsPerSec
            <= MeterDelayController::kServiceSlewCapMsPerSec);
    // And the packet-rate consequence, stated in the units that matter.
    const double dPrime = MeterDelayController::kSlewMsPerSec / 1000.0;
    QVERIFY(1.0 / (1.0 + dPrime) >= 0.90);
}

void MeterDelayControllerTests::alwaysOnEngagesWithNoShotInputAndNeverReleases()
{
    PolicyHarness h(MeterDelayController::EngagePolicy::AlwaysOn);
    QVERIFY(h.arm());

    // No physical edge, no bot hint, no possession signal — it engages purely
    // because the session armed.
    h.settle();
    const double target = h.c().targetDelayMs();
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), target);
    QVERIFY(h.c().conditionSettled());

    // And it stays there. kMaxEngagedMs (6s) is the stuck-state watchdog for
    // per-shot policies; for AlwaysOn a permanently held delay is the intent,
    // so the watchdog must not fire. Run well past it.
    h.tick(400);   // 20 s
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), target);
}

void MeterDelayControllerTests::alwaysOnHoldsOneConstantDelayAcrossManyShots()
{
    // THE lead-poisoning guard, and the reason AlwaysOn is the default.
    //
    // learned_latency_ms is a single scalar and the grader is frozen, so that
    // seed ships permanently. If the applied delay differs between shots the
    // lead is wrong by the difference — up to 165ms. This asserts that shot
    // activity cannot move the delay at all: same value, same condition key,
    // same epoch, from the first shot to the twentieth.
    PolicyHarness h(MeterDelayController::EngagePolicy::AlwaysOn);
    QVERIFY(h.arm());
    h.settle();

    const double settledDelay = h.c().currentDelayMs();
    const double settledKey = h.c().conditionKeyMs();
    const quint64 settledEpoch = h.c().conditionEpoch();
    QVERIFY(settledDelay > 0.0);

    for (int shot = 0; shot < 20; ++shot) {
        h.c().setPhysicalSquareHeld(true);
        h.tick(6);
        h.c().setPhysicalSquareHeld(false);
        h.c().setShotCycleActive(true);
        h.tick(10);
        h.c().setShotCycleActive(false);
        h.tick(8);

        QVERIFY2(std::abs(h.c().currentDelayMs() - settledDelay) < 1e-6,
                 qPrintable(QStringLiteral("shot %1 moved the delay to %2 (was %3)")
                                .arg(shot).arg(h.c().currentDelayMs()).arg(settledDelay)));
        QCOMPARE(h.c().conditionKeyMs(), settledKey);
        QCOMPARE(h.c().conditionEpoch(), settledEpoch);
        QVERIFY(h.c().conditionSettled());
    }
}

void MeterDelayControllerTests::deadBallWaitsForADeadBallThenHoldsLikeAlwaysOn()
{
    PolicyHarness h(MeterDelayController::EngagePolicy::DeadBall);
    QVERIFY(h.arm());

    // Ball is live: nothing engages, because a ramp here would be visible.
    h.tick(40);
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);

    // Dead ball — free throw, inbound, after a made basket. Nothing is moving,
    // so this is the one free moment to ramp.
    h.c().setBallLive(false);
    h.settle();
    const double target = h.c().targetDelayMs();
    QCOMPARE(h.c().currentDelayMs(), target);

    // Ball goes live again and the delay STAYS. Re-ramping per possession is
    // exactly the transition churn this policy exists to avoid.
    h.c().setBallLive(true);
    h.tick(100);
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), target);

    // Subsequent dead balls change nothing either.
    h.c().setBallLive(false);
    h.c().setBallLive(true);
    h.tick(40);
    QCOMPARE(h.c().currentDelayMs(), target);
}

// [ORION_DEFENSE_FLAG 2026-08-08] The D-pad Up hotkey flips ONLY the runtime
// defense flag. The persisted setting's runtime image (the engage policy) and
// the manual delay must be untouched by any number of toggles — the hotkey path
// in OrionAppController deliberately performs no config_ write (contrast the
// pre-fix hotkey, which flipped meterDelayBypassOnDefense and therefore
// SURVIVED RESTARTS: a customer could come back to a silently-bypassed delay).
void MeterDelayControllerTests::dpadUpToggleDrivesDefenseFlagOnly()
{
    PolicyHarness h(MeterDelayController::EngagePolicy::OffenseDefense);
    h.c().setManualDelayMs(250.0);
    QVERIFY(h.arm());
    QCOMPARE(h.c().defenseModeManualActive(), false); // launch default: offense

    h.c().setDefenseModeManualActive(true);
    QCOMPARE(h.c().defenseModeManualActive(), true);
    // The persisted-setting-derived state is untouched by the flag.
    QCOMPARE(h.c().engagePolicy(), MeterDelayController::EngagePolicy::OffenseDefense);
    QCOMPARE(h.c().manualDelayMs(), 250.0);
    QCOMPARE(h.c().enabled(), true);

    h.c().setDefenseModeManualActive(false);
    QCOMPARE(h.c().defenseModeManualActive(), false);
    QCOMPARE(h.c().engagePolicy(), MeterDelayController::EngagePolicy::OffenseDefense);
    QCOMPARE(h.c().manualDelayMs(), 250.0);

    // The inverse direction of the same contract: a policy change (the Option B
    // master switch flipping) resets the runtime flag rather than resurrecting
    // a stale defense latch.
    h.c().setDefenseModeManualActive(true);
    h.c().setEngagePolicy(MeterDelayController::EngagePolicy::AlwaysOn);
    QCOMPARE(h.c().defenseModeManualActive(), false);
}

// [ORION_DEFENSE_FLAG 2026-08-08] flag=false => engagement wanted => delay ramps
// to and holds target; flag=true => engagement refused => delay ramps to 0.
// The flag is THE bypass: no shot input, possession input or dead-ball input is
// involved in either direction.
void MeterDelayControllerTests::defenseFlagBypassesDelay()
{
    PolicyHarness h(MeterDelayController::EngagePolicy::OffenseDefense);
    h.c().setManualDelayMs(250.0);
    QVERIFY(h.arm());

    // Offense (default): engages purely because the session armed.
    h.settle();
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), 250.0);
    QVERIFY(h.c().conditionSettled());
    QVERIFY(h.c().readyForArm());

    // Defense: bypass. Ramps all the way to 0 at the slew cap, starting without
    // waiting out the minEngaged dwell (an explicit user command).
    h.c().setDefenseModeManualActive(true);
    const int slewTicks = static_cast<int>(
                              std::ceil(250.0 / MeterDelayController::kRampDownPerTickMs))
                          + 40;
    bool sawRampDown = false;
    for (int i = 0; i < slewTicks && h.c().currentDelayMs() > 0.0; ++i) {
        h.tick();
        if (h.c().shotState() == ShotState::Disengaging
            && h.c().currentDelayMs() < 250.0 - 0.5) {
            sawRampDown = true;
            QVERIFY2(!h.c().readyForArm(),
                     "fail-closed: arming must refuse while the delay is "
                     "genuinely ramping away from target");
        }
    }
    QVERIFY(sawRampDown);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    QCOMPARE(h.c().shotState(), ShotState::Standby);

    // Back to offense: re-engages and climbs back to target, and the monotonic
    // climb arms (readyForArm's OffenseDefense re-engage clause) — refusing
    // would open a multi-second dead window after every defensive possession.
    h.c().setDefenseModeManualActive(false);
    h.tick(4);
    QVERIFY(h.c().currentDelayMs() > 0.0);
    QVERIFY2(h.c().readyForArm(),
             "the monotonic re-engage climb must not disarm the engine");
    h.settle();
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), 250.0);
}

// [ORION_DEFENSE_FLAG 2026-08-08] The shot cycle is fully decoupled: it cannot
// engage a bypassed delay and it cannot bypass an engaged one. (It drove
// engagement for exactly one day — the 2026-08-08 morning fix — and proved
// unreliable; the manual flag replaced it.)
void MeterDelayControllerTests::shotCycleNoLongerDrivesBypass()
{
    PolicyHarness h(MeterDelayController::EngagePolicy::OffenseDefense);
    h.c().setManualDelayMs(250.0);
    QVERIFY(h.arm());

    // flag=false: delay applies, and the shot cycle changes nothing in either
    // direction.
    h.settle();
    QCOMPARE(h.c().currentDelayMs(), 250.0);
    const quint64 epoch = h.c().conditionEpoch();
    h.c().setShotCycleActive(true);
    h.tick(30);
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), 250.0);
    QCOMPARE(h.c().conditionEpoch(), epoch);
    h.c().setShotCycleActive(false);
    h.tick(30);
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), 250.0);
    QCOMPARE(h.c().conditionEpoch(), epoch);

    // flag=true: delay bypasses, and neither the shot-cycle hint NOR the raw
    // physical Square can re-engage it (the flag is the sole gate; a Square
    // held while defending is contest input, not a shot).
    h.c().setDefenseModeManualActive(true);
    for (int i = 0; i < 200 && h.c().currentDelayMs() > 0.0; ++i) h.tick();
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    h.c().setShotCycleActive(true);
    h.c().setPhysicalSquareHeld(true);
    h.tick(30);
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    h.c().setPhysicalSquareHeld(false);
    h.c().setShotCycleActive(false);
    h.tick(5);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
}

// [ORION_DEFENSE_FLAG 2026-08-08] The flag is NOT persisted and does not survive
// the session: disconnect resets it to offense, so a reconnect applies the delay
// again from scratch. A customer must never inherit "delay silently off".
void MeterDelayControllerTests::defenseFlagResetsOnSessionRestart()
{
    PolicyHarness h(MeterDelayController::EngagePolicy::OffenseDefense);
    h.c().setManualDelayMs(250.0);
    QVERIFY(h.arm());
    h.settle();
    QCOMPARE(h.c().currentDelayMs(), 250.0);

    // Defense, then the stream drops mid-defense.
    h.c().setDefenseModeManualActive(true);
    for (int i = 0; i < 200 && h.c().currentDelayMs() > 0.0; ++i) h.tick();
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    h.c().notifyStreamEnded();
    QCOMPARE(h.c().defenseModeManualActive(), false);

    // Reconnect: the delay applies again with no further input.
    QVERIFY(h.arm());
    h.settle();
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), 250.0);
}

void MeterDelayControllerTests::defenseReengageIsUnsettledDuringRamp()
{
    // The honest cost of the manual bypass: every defense -> offense flip is a
    // fresh ~2.5 s climb (250 ms at 100 ms/s), and a shot fired inside it reads
    // a partial delay. conditionSettled() must say so the whole way — the
    // release predicate gates on it. (readyForArm() deliberately accepts the
    // monotonic climb; that split is pinned in defenseFlagBypassesDelay.)
    PolicyHarness h(MeterDelayController::EngagePolicy::OffenseDefense);
    h.c().setManualDelayMs(250.0);
    QVERIFY(h.arm());
    h.settle();
    h.c().setDefenseModeManualActive(true);
    for (int i = 0; i < 200 && h.c().currentDelayMs() > 0.0; ++i) h.tick();
    QCOMPARE(h.c().currentDelayMs(), 0.0);

    h.c().setDefenseModeManualActive(false);
    // One second into the re-engage the ramp is still climbing.
    h.tick(20);   // 1000 ms
    QVERIFY(h.c().currentDelayMs() > 0.0);
    QVERIFY(h.c().currentDelayMs() < h.c().targetDelayMs());
    QVERIFY2(!h.c().conditionSettled(),
             "a shot taken here reads a partial delay and poisons the lead");

    h.settle();
    QVERIFY(h.c().conditionSettled());
}

void MeterDelayControllerTests::offenseHeldPastWatchdogNeverDisarmsTheEngine()
{
    // [D1 regression 2026-08-08] The shipped default config maps
    // meterDelayBypassOnDefense=true to OffenseDefense, for which
    // policyHoldsIndefinitely() is false, so the kMaxEngagedMs stuck-state
    // watchdog in shouldDisengage() is live. engagedAtMs_ is only reset by
    // beginEngagement(), which never runs while Locked, so 6 s of continuous
    // offense used to trip the watchdog: Locked -> Disengaging for exactly one
    // ~50 ms tick, publish() emitted delayConditionChanged(settled=false),
    // OrionAppController::syncEngineArmed() (wired to that signal) saw
    // readyForArm()==false and revoked any armed precise-fire token. current_
    // never moved -- a pure bookkeeping flip with a ~1% duty cycle that
    // silently killed 1-3 shots per 50-shot batch as
    // authority_lost_before_submit / live_tip_deadline_missed.
    // [ORION_DEFENSE_FLAG 2026-08-08] "Held offense" now means the manual
    // defense flag simply staying false (its default) — no possession input.
    PolicyHarness h(MeterDelayController::EngagePolicy::OffenseDefense);
    QVERIFY(h.arm());
    h.settle();
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    const double target = h.c().targetDelayMs();
    QVERIFY(h.c().conditionSettled());
    QVERIFY(h.c().readyForArm());

    QSignalSpy conditionSpy(&h.c(), &MeterDelayController::delayConditionChanged);
    QSignalSpy shotSpy(&h.c(), &MeterDelayController::shotStateChanged);

    // Continuous offense for kMaxEngagedMs + 2 s in ordinary 50 ms ticks --
    // well past the watchdog. The engagement is legitimate the entire time.
    const int ticks = static_cast<int>((MeterDelayController::kMaxEngagedMs + 2000)
                                       / MeterDelayController::kTickMs);
    for (int i = 0; i < ticks; ++i) {
        h.tick();
        QVERIFY2(h.c().shotState() == ShotState::Locked,
                 qPrintable(QStringLiteral(
                     "tick %1 (%2 ms of held offense): shot state left Locked -- "
                     "the watchdog fired against a wanted engagement")
                                .arg(i)
                                .arg((i + 1) * MeterDelayController::kTickMs)));
        QVERIFY2(h.c().readyForArm(),
                 qPrintable(QStringLiteral(
                     "tick %1: readyForArm() went false -- syncEngineArmed() "
                     "would revoke the armed precise-fire token here")
                                .arg(i)));
        QVERIFY2(h.c().conditionSettled(),
                 qPrintable(QStringLiteral("tick %1: condition unsettled").arg(i)));
        QCOMPARE(h.c().currentDelayMs(), target);
    }
    // Stronger than the per-tick probes: no shot-state transition happened at
    // all, and no unsettled condition was ever PUBLISHED. Every
    // delayConditionChanged lands in OrionAppController::syncEngineArmed(); a
    // settled=false emission here IS the engine disarm.
    QCOMPARE(shotSpy.count(), 0);
    for (const QList<QVariant>& args : conditionSpy) {
        QVERIFY2(args.at(2).toBool(),
                 "delayConditionChanged(settled=false) was emitted during "
                 "continuously held offense -- this edge disarms the engine and "
                 "kills any in-flight shot");
    }

    // Fail-closed must be preserved: flipping to defense still ramps down, and
    // the arm gate refuses from the moment the delay genuinely leaves target.
    h.c().setDefenseModeManualActive(true);
    bool sawUnsettledRampDown = false;
    for (int i = 0; i < 200 && h.c().shotState() != ShotState::Standby; ++i) {
        h.tick();
        if (h.c().shotState() == ShotState::Disengaging
            && h.c().currentDelayMs() < target - 0.5) {
            sawUnsettledRampDown = true;
            QVERIFY2(!h.c().readyForArm(),
                     "readyForArm() must refuse while the delay is genuinely "
                     "ramping away from target");
            QVERIFY(!h.c().conditionSettled());
        }
    }
    QVERIFY2(sawUnsettledRampDown,
             "the offense drop never produced an observable ramp-down");
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
}

void MeterDelayControllerTests::shotGatedReportsThatItCannotSettleInTime()
{
    PolicyHarness h(MeterDelayController::EngagePolicy::ShotGated);
    QVERIFY(h.arm());
    // Not a silent half-working state: the controller says so, and the reason
    // text carries the numbers.
    QVERIFY(!h.c().policyCanSettleBeforeMeter());
    QVERIFY(h.c().reasonText().contains(QStringLiteral("WARNING")));
    QVERIFY(h.c().reasonText().contains(QStringLiteral("conditionSettled")));

    // Every other policy can, because they engage long before any shot.
    for (auto policy : {MeterDelayController::EngagePolicy::AlwaysOn,
                        MeterDelayController::EngagePolicy::DeadBall,
                        MeterDelayController::EngagePolicy::OffenseDefense}) {
        PolicyHarness other(policy);
        QVERIFY(other.arm());
        QVERIFY(other.c().policyCanSettleBeforeMeter());
    }
}

void MeterDelayControllerTests::policyChangeCannotStrandAHeldDelay()
{
    // Switching from a policy that holds to one that does not must release the
    // delay rather than leaving it pinned with nothing asking for it.
    // [ORION_DEFENSE_FLAG 2026-08-08] The strand target is now ShotGated:
    // OffenseDefense engages by default (its manual defense flag starts false),
    // so it no longer represents "wants nothing".
    PolicyHarness h(MeterDelayController::EngagePolicy::AlwaysOn);
    QVERIFY(h.arm());
    h.settle();
    QVERIFY(h.c().currentDelayMs() > 0.0);

    h.c().setEngagePolicy(MeterDelayController::EngagePolicy::ShotGated);
    // No physical shot input has been given, so ShotGated wants nothing.
    for (int i = 0; i < 200 && h.c().currentDelayMs() > 0.0; ++i) h.tick();
    QCOMPARE(h.c().currentDelayMs(), 0.0);
    QCOMPARE(h.c().shotState(), ShotState::Standby);
}

void MeterDelayControllerTests::deadBallLatchDoesNotSurviveTheSession()
{
    PolicyHarness h(MeterDelayController::EngagePolicy::DeadBall);
    QVERIFY(h.arm());
    h.c().setBallLive(false);
    h.settle();
    QVERIFY(h.c().currentDelayMs() > 0.0);

    // Session ends. A new session must wait for its OWN dead ball rather than
    // inheriting the last one's latch and ramping while play is live.
    h.c().notifyStreamEnded();
    QCOMPARE(h.c().currentDelayMs(), 0.0);

    QVERIFY(h.arm());
    h.tick(60);
    QCOMPARE(h.c().shotState(), ShotState::Standby);
    QCOMPARE(h.c().currentDelayMs(), 0.0);
}

// [ORION_DEFENSE_FLAG 2026-08-08] bypassOnDefenseRampsToZero and
// bypassOnDefenseShotCycleDrivesEngagement were DELETED here, not merely
// updated: both pinned the one-day shot-cycle-driven bypass wiring that the
// manual defense flag replaced. Their live properties moved to
// defenseFlagBypassesDelay (ramp-to-zero + re-engage arm clause) and
// shotCycleNoLongerDrivesBypass (the explicit reversal of the hint-start
// behaviour the second test existed to pin).

void MeterDelayControllerTests::midLockTargetChangeIsADelayGateDisarmOnly()
{
    // [ORION_METER_DELAY_ROUTE_AUTHORITY 2026-08-08] Mocked live sequence
    // (owner rig, orion_native.log 22:32Z): OffenseDefense policy, manual
    // target 250 ms, intercept Locked with applied_ms=250 (> 0), user raises
    // the target to 300 ms mid-lock. The 50 ms/tick re-ramp publishes
    // settled=false, readyForArm() refuses -- CORRECT, the visible meter is
    // time-shifting under any armed token -- but syncEngineArmed() used to map
    // that refusal to a full route-attestation revocation, benching every press
    // as waiting_for_latency_calibration for 72 s until a neutral controller
    // window re-issued a route proof. The disarm cause must stay classified as
    // delay-gate-only so the attestation survives and authority returns the
    // moment the ramp settles.
    PolicyHarness h(MeterDelayController::EngagePolicy::OffenseDefense);
    h.c().setManualDelayMs(250.0);
    QVERIFY(h.arm());
    h.c().setOffense(true);
    h.settle();
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), 250.0);
    QVERIFY(h.c().conditionSettled());
    QVERIFY(h.c().readyForArm());
    const quint64 lockedEpoch = h.c().conditionEpoch();

    // The live 250 -> 300 edge.
    h.c().setManualDelayMs(300.0);
    h.tick();
    QVERIFY2(h.c().currentDelayMs() > 0.0,
             "the applied delay must stay nonzero between two nonzero targets");
    QVERIFY(!h.c().conditionSettled());
    QVERIFY2(!h.c().readyForArm(),
             "the ramp between two applied targets must refuse arming "
             "(an armed token would fire against a time-shifting meter)");

    // The refusal reaches syncEngineArmed() with route authority fully intact,
    // so it must classify as a delay-gate-only disarm: no attestation
    // revocation (see AutomationEngineTests::
    // meterDelayRampDisarmPreservesRouteAttestation for the full truth table).
    {
        const orion::EngineArmDecision d = orion::engineArmDecision(
            /*routeAuthorityOk=*/true, h.c().readyForArm());
        QVERIFY(!d.arm);
        QVERIFY2(!d.revokeRouteAttestation,
                 "mid-lock target change revoked the controller-route "
                 "attestation -- waiting_for_latency_calibration bench");
    }

    // Ramp out: settles at the new target, gate reopens, condition epoch
    // advanced (per-shot dating stays honest), and the engine may re-arm on the
    // preserved attestation with no fresh route proof.
    h.settle();
    QCOMPARE(h.c().shotState(), ShotState::Locked);
    QCOMPARE(h.c().currentDelayMs(), 300.0);
    QVERIFY(h.c().conditionSettled());
    QVERIFY(h.c().readyForArm());
    QVERIFY(h.c().conditionEpoch() > lockedEpoch);
    QVERIFY(orion::engineArmDecision(true, h.c().readyForArm()).arm);
}

QTEST_MAIN(MeterDelayControllerTests)
#include "MeterDelayControllerTests.moc"
