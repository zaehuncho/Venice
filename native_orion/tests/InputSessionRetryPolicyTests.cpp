// [ORION_INPUT_DEAD_UX 2026-08-30] Contract tests for the dead-input fix:
//   * pressUndeliverable() — the ONE predicate behind both the per-press
//     "PRESS UNDELIVERABLE" forensic line and the on-screen overlay;
//   * inputDeadOverlaySeverity() — the overlay's visibility verdict;
//   * InputSessionRetryPlanner — the bounded, backoff-spaced auto-retry state
//     machine (gives up cleanly, never spins, never retries a security block,
//     cold-restarts every no-verdict class so a queued start_stream can never
//     resurrect a condemned session).
// Pure policy: no sockets, no processes, no timers.

#include "ControllerRoutingPolicy.h"
#include "InputSessionRetryPolicy.h"

#include <QtTest/QTest>

using namespace orion;

class InputSessionRetryPolicyTests final : public QObject {
    Q_OBJECT

private slots:
    void pendingRecoveryBlocksWritesEvenWhileVideoRemainsRunning_data()
    {
        QTest::addColumn<int>("stateValue");
        QTest::addColumn<bool>("pending");
        QTest::addColumn<bool>("expected");
        for (int state = 0; state < 4; ++state) {
            for (bool pending : {false, true}) {
                const QByteArray name = QByteArray::number(state) + (pending ? "-recovering" : "-settled");
                QTest::newRow(name.constData()) << state << pending
                    << (static_cast<RemotePlayState>(state) == RemotePlayState::Running && !pending);
            }
        }
    }

    void pendingRecoveryBlocksWritesEvenWhileVideoRemainsRunning()
    {
        QFETCH(int, stateValue);
        QFETCH(bool, pending);
        QFETCH(bool, expected);
        QCOMPARE(directInputWriteAllowed(static_cast<RemotePlayState>(stateValue), pending), expected);
    }

    void repeatedRecoveryDoesNotBorrowPreviousRunningAuthority()
    {
        // Exercise the actual idle-neutral/write predicate across successive
        // recovery attempts. No timer or pipe existence is a readiness proof.
        for (int attempt = 0; attempt < 3; ++attempt) {
            for (int idleTick = 0; idleTick < 10000; ++idleTick)
                QVERIFY(!directInputWriteAllowed(RemotePlayState::Running, true));
            QVERIFY(directInputWriteAllowed(RemotePlayState::Running, false));
        }
        QVERIFY(!directInputWriteAllowed(RemotePlayState::Error, false));
        QVERIFY(!directInputWriteAllowed(RemotePlayState::Disconnected, false));
    }

    // ── pressUndeliverable: mirrors the fail-closed write gate exactly ──────

    void pressUndeliverableMatchesWriteGateOnSessionState()
    {
        // Any non-Running session refuses writes (directInputWriteAllowed), so a
        // press is undeliverable regardless of pipe flags. This is the measured
        // incident class: 31/185 Square edges during Disconnected, 08-28..08-30.
        const RemotePlayState deadStates[] = {
            RemotePlayState::Disconnected,
            RemotePlayState::Connecting,
            RemotePlayState::Error,
        };
        for (const RemotePlayState state : deadStates) {
            QVERIFY(!directInputWriteAllowed(state));
            QVERIFY(pressUndeliverable(state, false, false));
            QVERIFY(pressUndeliverable(state, true, true));
            QVERIFY(pressUndeliverable(state, true, false));
        }
    }

    void pressDeliverableOnlyWhenRunningWithHealthyRoute()
    {
        // Running + pipe route healthy (or pipe route not in use) = deliverable.
        QVERIFY(!pressUndeliverable(RemotePlayState::Running, true, true));
        QVERIFY(!pressUndeliverable(RemotePlayState::Running, false, false));
        QVERIFY(!pressUndeliverable(RemotePlayState::Running, false, true));
        // Running while the enabled direct pipe is down: the authoritative route
        // is dead even though the session says Running.
        QVERIFY(pressUndeliverable(RemotePlayState::Running, true, false));
    }

    // ── overlay severity: video-alive + intent/press gating ─────────────────

    void overlayHiddenWhenInputDeliverable()
    {
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Running, true, true,
                                          true, true, 1000, 900),
                 InputDeadSeverity::None);
    }

    void overlayHiddenWithoutLiveVideo()
    {
        // No pixels, no illusion: the page's ordinary Disconnected/Error state
        // owns that surface.
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Error, false, false,
                                          /*videoAlive=*/false, true, 1000, 900),
                 InputDeadSeverity::None);
    }

    void overlayHiddenDuringQuietPreviewBrowse()
    {
        // Warm preview before Connect, no session intent, no press: stay quiet.
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Disconnected,
                                          false, false,
                                          true, /*intent=*/false,
                                          1000, /*lastPress=*/0),
                 InputDeadSeverity::None);
    }

    void overlayCriticalForFailedSessionUnderLiveVideo()
    {
        // The 08-28 21:48 incident shape: promote failed (Error), warm preview
        // restored (video alive), player had pressed Connect (intent).
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Error, false, false,
                                          true, true, 1000, 0),
                 InputDeadSeverity::Critical);
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Disconnected,
                                          false, false, true, true, 1000, 0),
                 InputDeadSeverity::Critical);
    }

    void overlayCriticalWhenRunningPipeDown()
    {
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Running, true, false,
                                          true, true, 1000, 0),
                 InputDeadSeverity::Critical);
    }

    void overlayNoticeWhileConnecting()
    {
        // Connecting is honestly transitional: calm note, not a red alarm.
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Connecting,
                                          false, false, true, true, 1000, 0),
                 InputDeadSeverity::Notice);
    }

    void undeliverablePressLatchesOverlayWithoutIntent()
    {
        // A press into dead input proves the player believes they are connected
        // — the overlay must appear even in the intent-less preview browse …
        const qint64 pressMs = 10'000;
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Disconnected,
                                          false, false, true, false,
                                          pressMs + kUndeliverablePressLatchMs,
                                          pressMs),
                 InputDeadSeverity::Critical);
        // … and expire once the latch window passes.
        QCOMPARE(inputDeadOverlaySeverity(RemotePlayState::Disconnected,
                                          false, false, true, false,
                                          pressMs + kUndeliverablePressLatchMs + 1,
                                          pressMs),
                 InputDeadSeverity::None);
    }

    void overlayAndPressLineShareOneCondition()
    {
        // One source of truth: whenever the press-line predicate is FALSE the
        // overlay must be None no matter what the visibility gates say — the
        // screen can never claim dead input while the log would not.
        for (int stateInt = 0; stateInt <= 3; ++stateInt) {
            const auto state = static_cast<RemotePlayState>(stateInt);
            for (int pe = 0; pe <= 1; ++pe) {
                for (int pc = 0; pc <= 1; ++pc) {
                    if (pressUndeliverable(state, pe != 0, pc != 0)) {
                        continue;
                    }
                    QCOMPARE(inputDeadOverlaySeverity(state, pe != 0, pc != 0,
                                                      true, true, 1000, 999),
                             InputDeadSeverity::None);
                }
            }
        }
    }

    void runningEdgeDebounceSuppressesConnectFlashOnly()
    {
        // The pipe seeds a beat after Running: within the grace, no alarm …
        QVERIFY(!runningPipeDownConfirmed(/*now=*/1000, /*downSince=*/1000,
                                          /*lastPress=*/0));
        QVERIFY(!runningPipeDownConfirmed(1000 + kRunningPipeDownOverlayGraceMs - 1,
                                          1000, 0));
        // … a persisted down state alarms …
        QVERIFY(runningPipeDownConfirmed(1000 + kRunningPipeDownOverlayGraceMs,
                                         1000, 0));
        // … and an actual undeliverable press inside the grace is ground truth
        // that bypasses the debounce on the same tick.
        QVERIFY(runningPipeDownConfirmed(1200, 1000, /*lastPress=*/1100));
        // A press from BEFORE this down episode proves nothing about it.
        QVERIFY(!runningPipeDownConfirmed(1200, 1000, /*lastPress=*/900));
        // Healthy pipe (no down episode) never confirms.
        QVERIFY(!runningPipeDownConfirmed(999'999, 0, 999'998));
    }

    // ── retry planner: bounded, classified, clean give-up ───────────────────

    void plannerBacksOffAndGivesUpCleanly()
    {
        InputSessionRetryPlanner planner;
        qint64 lastDelay = 0;
        for (int i = 1; i <= InputSessionRetryPlanner::kMaxAttempts; ++i) {
            const InputSessionRetryDecision d = planner.onFailure(
                InputSessionFailureClass::SidecarVerdict, false);
            QVERIFY(d.retry);
            QCOMPARE(d.attempt, i);
            QVERIFY2(d.delayMs > lastDelay, "backoff must strictly grow");
            QVERIFY2(d.delayMs >= 2000, "never a hot loop");
            lastDelay = d.delayMs;
        }
        // Attempt N+1 gives up — cleanly and permanently until reset.
        QVERIFY(!planner.onFailure(InputSessionFailureClass::SidecarVerdict, false).retry);
        QVERIFY(planner.exhausted());
        QVERIFY(!planner.onFailure(InputSessionFailureClass::SidecarVerdict, false).retry);
    }

    void verdictFailuresRetryWarmNoVerdictFailuresRetryCold()
    {
        // SidecarVerdict: the verdict event proves the sidecar's stdin loop has
        // returned, so an in-place warm re-promotion cannot queue behind a wedge.
        InputSessionRetryPlanner planner;
        QVERIFY(!planner.onFailure(InputSessionFailureClass::SidecarVerdict,
                                   false).coldRestart);
        // Local refusal launched nothing: warm.
        QVERIFY(!planner.onFailure(InputSessionFailureClass::LocalRefusal,
                                   false).coldRestart);
        // DeadlineTimeout / CommandUndeliverable delivered NO verdict — the
        // sidecar may be wedged inside the promotion, and a warm start_stream
        // would queue behind it (double-spawn/orphan hazard). Must be cold.
        QVERIFY(planner.onFailure(InputSessionFailureClass::DeadlineTimeout,
                                  false).coldRestart);
        QVERIFY(planner.onFailure(InputSessionFailureClass::CommandUndeliverable,
                                  false).coldRestart);
    }

    void identityBlockNeverRetries()
    {
        // A trusted-client-image failure is a security refusal: retrying cannot
        // heal it and must never loop over it.
        InputSessionRetryPlanner planner;
        const InputSessionRetryDecision d = planner.onFailure(
            InputSessionFailureClass::IdentityBlocked, false);
        QVERIFY(!d.retry);
        QVERIFY(planner.identityBlocked());
        QVERIFY(planner.exhausted());
        // …and it also blocks the press-triggered path.
        QVERIFY(!planner.onUndeliverablePress(999'999).retry);
    }

    void wakeObservedFailureWaitsOutTheConsoleBoot()
    {
        // Respect the rest-mode wake path: a wake-observed failure means the
        // console may still be booting — wait longer, never fight the budget.
        InputSessionRetryPlanner awake;
        InputSessionRetryPlanner waking;
        const qint64 base = awake.onFailure(
            InputSessionFailureClass::DeadlineTimeout, false).delayMs;
        const qint64 extended = waking.onFailure(
            InputSessionFailureClass::DeadlineTimeout, true).delayMs;
        QCOMPARE(extended - base, InputSessionRetryPlanner::kWakeExtraDelayMs);
    }

    void resetRestoresAFullBudget()
    {
        InputSessionRetryPlanner planner;
        for (int i = 0; i <= InputSessionRetryPlanner::kMaxAttempts; ++i) {
            (void)planner.onFailure(InputSessionFailureClass::SidecarVerdict, false);
        }
        QVERIFY(planner.exhausted());
        planner.reset();   // Running reached, or a fresh MANUAL Connect/Disconnect
        QVERIFY(!planner.exhausted());
        QCOMPARE(planner.attemptsUsed(), 0);
        QVERIFY(planner.onFailure(InputSessionFailureClass::SidecarVerdict, false).retry);
    }

    void pressTriggeredRetryOnlyAfterExhaustionAndThrottled()
    {
        InputSessionRetryPlanner planner;
        // While the scheduled budget is live, a press must NOT add attempts —
        // the pending backoff retry owns recovery (no reconnect spam).
        (void)planner.onFailure(InputSessionFailureClass::SidecarVerdict, false);
        QVERIFY(!planner.onUndeliverablePress(1000).retry);

        // Exhaust the budget.
        while (planner.onFailure(InputSessionFailureClass::SidecarVerdict, false).retry) {
        }
        QVERIFY(planner.exhausted());

        // Now the press earns exactly ONE prompt attempt …
        const InputSessionRetryDecision first = planner.onUndeliverablePress(10'000);
        QVERIFY(first.retry);
        QVERIFY(first.delayMs <= 1000);   // prompt — the player just asked
        // … further presses inside the throttle window earn nothing …
        QVERIFY(!planner.onUndeliverablePress(10'500).retry);
        QVERIFY(!planner.onUndeliverablePress(
            10'000 + InputSessionRetryPlanner::kPressRetryThrottleMs - 1).retry);
        // … and the next one only after the full throttle window.
        QVERIFY(planner.onUndeliverablePress(
            10'000 + InputSessionRetryPlanner::kPressRetryThrottleMs).retry);
    }

    void pressTriggeredRetryReusesLastFailureClassRestartMode()
    {
        // After a no-verdict failure the press-triggered attempt must stay COLD:
        // exhaustion does not make a possibly-wedged sidecar safe to warm-poke.
        InputSessionRetryPlanner planner;
        for (int i = 0; i <= InputSessionRetryPlanner::kMaxAttempts; ++i) {
            (void)planner.onFailure(InputSessionFailureClass::DeadlineTimeout, false);
        }
        QVERIFY(planner.exhausted());
        const InputSessionRetryDecision d = planner.onUndeliverablePress(5'000'000);
        QVERIFY(d.retry);
        QVERIFY(d.coldRestart);
    }

    // ── [ORION_CONTROLLER_UI_ISOLATION 2026-09-19] ──────────────────────────
    // Owner defect: "when using the controller you can see it moving on the pc
    // hovering over stuff". The 2026-09-11 guard only ever classified mapped
    // navigation KEYS and mouse BUTTONS, and only inside a 120 ms window opened
    // by a button/D-pad/trigger EDGE. A stick mapped to the desktop pointer
    // produces neither, so the cursor walked over Venice unopposed.

    void pointerMotionWasInvisibleToTheShippedPressGuard()
    {
        // Regression anchor for the actual defect: even with the press guard
        // wide open, the OLD predicate could not suppress pointer motion or the
        // wheel, because it only ever answered for two kinds.
        QVERIFY(!shouldSuppressControllerMappedUiInput(
            true, 1000, 5000, DesktopUiInputKind::PointerMotion));
        QVERIFY(!shouldSuppressControllerMappedUiInput(
            true, 1000, 5000, DesktopUiInputKind::WheelScroll));
        // …and the shipped behaviour for the two kinds it does answer for is
        // preserved byte-for-byte by the new decision.
        QVERIFY(shouldIsolateDesktopUiInput(
            true, false, false, DesktopUiInputOrigin::Hardware,
            1000, 5000, 0, DesktopUiInputKind::NavigationKey));
        QVERIFY(shouldIsolateDesktopUiInput(
            true, false, false, DesktopUiInputOrigin::Hardware,
            1000, 5000, 0, DesktopUiInputKind::MouseButton));
    }

    void stickDeflectionOpensThePointerGuardAndPressEdgesDoNot()
    {
        // normalizeHidAxis() already zeroes |axis| < 8, so resting noise is 0.
        QVERIFY(!controllerUiPointerActivity(0, 0, 0, 0));
        QVERIFY(!controllerUiPointerActivity(
            kControllerUiStickDeflection - 1, 0, 0, 0));
        QVERIFY(controllerUiPointerActivity(kControllerUiStickDeflection, 0, 0, 0));
        QVERIFY(controllerUiPointerActivity(0, -kControllerUiStickDeflection, 0, 0));
        QVERIFY(controllerUiPointerActivity(0, 0, 127, 0));
        QVERIFY(controllerUiPointerActivity(0, 0, 0, -127));
        // A pure button press is NOT pointer activity: the two guards stay
        // separate so a click cannot silently mute the real mouse's hover.
        QVERIFY(!controllerUiPointerActivity(0, 0, 0, 0));
        QVERIFY(controllerUiActivityPressEdge(1, 8, 0, 0, 0, 8, 0, 0));
    }

    void pointerGuardSuppressesMotionAndWheelWhileStreaming()
    {
        // Guard open (stick deflected within the last kControllerUiPointerGuardMs).
        QVERIFY(shouldIsolateDesktopUiInput(
            true, false, false, DesktopUiInputOrigin::Hardware,
            1000, 0, 1100, DesktopUiInputKind::PointerMotion));
        QVERIFY(shouldIsolateDesktopUiInput(
            true, false, false, DesktopUiInputOrigin::Hardware,
            1000, 0, 1100, DesktopUiInputKind::WheelScroll));
        // Guard closed: the owner's real mouse is untouched.
        QVERIFY(!shouldIsolateDesktopUiInput(
            true, false, false, DesktopUiInputOrigin::Hardware,
            1101, 0, 1100, DesktopUiInputKind::PointerMotion));
        QVERIFY(!shouldIsolateDesktopUiInput(
            true, false, false, DesktopUiInputOrigin::Hardware,
            1101, 0, 1100, DesktopUiInputKind::WheelScroll));
        // The press guard and the pointer guard do not cross-feed.
        QVERIFY(!shouldIsolateDesktopUiInput(
            true, false, false, DesktopUiInputOrigin::Hardware,
            1000, 5000, 0, DesktopUiInputKind::PointerMotion));
        QVERIFY(!shouldIsolateDesktopUiInput(
            true, false, false, DesktopUiInputOrigin::Hardware,
            1000, 0, 5000, DesktopUiInputKind::NavigationKey));
    }

    void injectedOriginSuppressesEveryUiKindWithNoTimingWindow()
    {
        // The leg that closes the ordering race: a mapper's SendInput message can
        // be dispatched before the 4 ms input poll observes the edge that would
        // have opened the guard. Both guards are CLOSED here (0) and it is still
        // suppressed, because injected-while-streaming is proof on its own.
        const DesktopUiInputKind kinds[] = {
            DesktopUiInputKind::NavigationKey,
            DesktopUiInputKind::MouseButton,
            DesktopUiInputKind::PointerMotion,
            DesktopUiInputKind::WheelScroll,
        };
        for (const DesktopUiInputKind kind : kinds) {
            QVERIFY(shouldIsolateDesktopUiInput(
                true, false, true, DesktopUiInputOrigin::Injected,
                9'999'999, 0, 0, kind));
            // Hardware-sourced input with both guards shut is never touched.
            QVERIFY(!shouldIsolateDesktopUiInput(
                true, false, true, DesktopUiInputOrigin::Hardware,
                9'999'999, 0, 0, kind));
            // Unknown origin (API unavailable) falls back to the guards, never
            // to "suppress".
            QVERIFY(!shouldIsolateDesktopUiInput(
                true, false, true, DesktopUiInputOrigin::Unknown,
                9'999'999, 0, 0, kind));
        }
    }

    void hardwareInputSurvivesActiveControllerTimingGuards()
    {
        for (const auto kind : {DesktopUiInputKind::NavigationKey,
                               DesktopUiInputKind::MouseButton,
                               DesktopUiInputKind::PointerMotion,
                               DesktopUiInputKind::WheelScroll}) {
            QVERIFY(!shouldIsolateDesktopUiInput(
                true, false, true, DesktopUiInputOrigin::Hardware,
                1000, 2000, 2000, kind));
            QVERIFY(shouldIsolateDesktopUiInput(
                true, false, true, DesktopUiInputOrigin::Injected,
                1000, 2000, 2000, kind));
            QVERIFY(shouldIsolateDesktopUiInput(
                true, false, true, DesktopUiInputOrigin::Unknown,
                1000, 2000, 2000, kind));
        }
    }

    void injectedIsolationKnobOffLeavesOnlyTheTimingGuards()
    {
        QVERIFY(!shouldIsolateDesktopUiInput(
            true, false, /*injectedIsolationEnabled=*/false,
            DesktopUiInputOrigin::Injected, 9'999'999, 0, 0,
            DesktopUiInputKind::PointerMotion));
        QVERIFY(shouldIsolateDesktopUiInput(
            true, false, /*injectedIsolationEnabled=*/false,
            DesktopUiInputOrigin::Injected, 1000, 0, 1100,
            DesktopUiInputKind::PointerMotion));
    }

    void isolationNeverEngagesOutsideASessionOrInPassthroughMode()
    {
        const DesktopUiInputKind kinds[] = {
            DesktopUiInputKind::Other,
            DesktopUiInputKind::NavigationKey,
            DesktopUiInputKind::MouseButton,
            DesktopUiInputKind::PointerMotion,
            DesktopUiInputKind::WheelScroll,
        };
        for (const DesktopUiInputKind kind : kinds) {
            // No live session: the pad is not gameplay input, hands off.
            QVERIFY(!shouldIsolateDesktopUiInput(
                /*streamActive=*/false, false, true,
                DesktopUiInputOrigin::Injected, 1000, 5000, 5000, kind));
            // Explicit controller-UI mode (ORION_CONTROLLER_UI_PASSTHROUGH=1 or a
            // deliberate configure-controller flow): hands off even mid-session.
            QVERIFY(!shouldIsolateDesktopUiInput(
                true, /*controllerUiModeActive=*/true, true,
                DesktopUiInputOrigin::Injected, 1000, 5000, 5000, kind));
        }
        // "Other" is never suppressed even in the worst case: ordinary typing,
        // WM_PAINT, everything that is not a UI-driving message.
        QVERIFY(!shouldIsolateDesktopUiInput(
            true, false, true, DesktopUiInputOrigin::Injected,
            1000, 5000, 5000, DesktopUiInputKind::Other));
        QVERIFY(!desktopUiInputKindDrivesUi(DesktopUiInputKind::Other));
        QVERIFY(desktopUiInputKindDrivesUi(DesktopUiInputKind::PointerMotion));
    }

    // ── [ORION_DISCONNECT_AUDIT 2026-09-19] lifecycle guards ────────────────

    void aRetiredSidecarExitIsIgnoredOnlyWhenANewerOneIsLive()
    {
        // The NORMAL teardown must keep running the full path: stopSidecar() nulls
        // sidecarProcess_ before it waits, so an in-call finished sees "no live
        // process" and still has to classify the exit and emit sidecarExited().
        QVERIFY(!sidecarExitBelongsToRetiredGeneration(
            /*haveLiveSidecarProcess=*/false, /*liveSidecarProcessIsThisOne=*/false));
        // The live process is the one that exited: the ordinary crash/stop path.
        QVERIFY(!sidecarExitBelongsToRetiredGeneration(true, true));
        // A DIFFERENT process is installed -> this exit is from a retired generation.
        // Letting it through nulls the live pointer (every later sendSidecarCommand
        // silently fails), latches rejectLateSidecarStarted_ against the new sidecar's
        // `started` (Connecting until the promote deadline) and emits a spurious exit.
        QVERIFY(sidecarExitBelongsToRetiredGeneration(true, false));
    }

    void deferredHandoffNeedsBothTheIntentAndTheConnectingState()
    {
        QVERIFY(deferredSidecarHandoffStillCurrent(7, 7, RemotePlayState::Connecting));
        // A later start()/stop() retired this handoff.
        QVERIFY(!deferredSidecarHandoffStillCurrent(7, 8, RemotePlayState::Connecting));
        // Right intent, wrong state (a stop landed): still stands down.
        QVERIFY(!deferredSidecarHandoffStillCurrent(7, 7, RemotePlayState::Disconnected));
        QVERIFY(!deferredSidecarHandoffStillCurrent(7, 7, RemotePlayState::Running));
        QVERIFY(!deferredSidecarHandoffStillCurrent(7, 7, RemotePlayState::Error));
        // The state check alone was the shipped guard and is NOT sufficient: a
        // superseded connect leaves exactly the same level.
        QVERIFY(!deferredSidecarHandoffStillCurrent(7, 9, RemotePlayState::Connecting));
    }

    void aSidecarRestartMayNeverRaceATeardownOrShutdown()
    {
        QVERIFY(sidecarRestartAllowedDuringLifecycle(false, false));
        QVERIFY(!sidecarRestartAllowedDuringLifecycle(true, false));
        // The case the audit added: a user Disconnect is in progress.
        QVERIFY(!sidecarRestartAllowedDuringLifecycle(false, true));
        QVERIFY(!sidecarRestartAllowedDuringLifecycle(true, true));
    }

    void pointerGuardOutlivesTheStickByTheMapperSmoothingTail()
    {
        // Documented contract: the pointer guard is deliberately LONGER than the
        // 120 ms press guard because a mapped pointer keeps emitting motion after
        // the stick recenters.
        QVERIFY(kControllerUiPointerGuardMs > kControllerUiGuardMs);
    }
};

QTEST_APPLESS_MAIN(InputSessionRetryPolicyTests)

#include "InputSessionRetryPolicyTests.moc"
