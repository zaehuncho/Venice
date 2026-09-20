#include "OrionInputClient.h"
#include "LatencyCalibrationSetupPolicy.h"
#include "PressedOverlayPolicy.h"
#include "LiveMeterHudPolicy.h"
#include "ReleaseMarkerDeliveryGate.h"
#include "ReleaseMarkerProtocol.h"
#include "ShotIntentPolicy.h"
#include "SidecarLogRelayPolicy.h"
#include "SidecarWatchdog.h"
#include "SquareOutputWatchdog.h"

#include <QtTest/QTest>

#include <atomic>
#include <future>
#include <limits>
#include <thread>
#include <vector>

using namespace orion;

namespace {

constexpr uint32_t kSquare = 1u << 2;

bool hasFlag(uint8_t flags, OrionInputPacketFlag flag)
{
    return (flags & static_cast<uint8_t>(flag)) != 0;
}

OrionInputPacket ownedIdle()
{
    OrionInputPacket packet{};
    packet.own = 1;
    return packet;
}

} // namespace

class OrionInputProtocolTests final : public QObject {
    Q_OBJECT

private slots:

    void squareWatchdogPipeCopies_data()
    {
        QTest::addColumn<int>("missingAck");
        QTest::newRow("two-confirmed-copies") << -1;
        QTest::newRow("first-copy-ambiguous") << 1;
        QTest::newRow("second-copy-ambiguous") << 2;
    }
    void squareWatchdogPipeCopies()
    {
#ifdef _WIN32
        QFETCH(int, missingAck);
        const QByteArray name = QByteArray("\\\\.\\pipe\\orion_watchdog_")
            + QByteArray::number(GetCurrentProcessId()) + '_'
            + QByteArray::number(GetTickCount64());
        std::promise<void> readyPromise;
        auto ready = readyPromise.get_future();
        std::atomic<DWORD> error{ERROR_SUCCESS};
        std::vector<OrionInputPacket> packets;
        std::thread server([&]() {
            HANDLE pipe = CreateNamedPipeA(name.constData(), PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                1, sizeof(OrionInputAck) * 8, sizeof(OrionInputPacket) * 8, 0, nullptr);
            if (pipe == INVALID_HANDLE_VALUE) {
                error.store(GetLastError()); readyPromise.set_value(); return;
            }
            readyPromise.set_value();
            if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
                error.store(GetLastError()); CloseHandle(pipe); return;
            }
            for (int i = 0; i < 3; ++i) {
                OrionInputPacket packet{};
                DWORD count = 0;
                if (!ReadFile(pipe, &packet, sizeof(packet), &count, nullptr)
                    || count != sizeof(packet)) { error.store(ERROR_READ_FAULT); break; }
                packets.push_back(packet);
                if (i == missingAck) { Sleep(200); break; }
                OrionInputAck ack{};
                ack.magic = kOrionInputAckMagic;
                ack.sourceSeq = packet.seq;
                ack.stage = static_cast<uint8_t>(OrionInputAckStage::LocalUdpAccepted);
                if (!WriteFile(pipe, &ack, sizeof(ack), &count, nullptr)
                    || count != sizeof(ack)) { error.store(ERROR_WRITE_FAULT); break; }
                FlushFileBuffers(pipe);
            }
            DisconnectNamedPipe(pipe); CloseHandle(pipe);
        });
        ready.wait();
        if (error.load() != ERROR_SUCCESS) {
            server.join(); QFAIL("watchdog test pipe creation failed");
        }
        OrionInputClient client(QString::fromLatin1(name));
        client.setEnabled(true);
        const auto before = client.releaseSquareForWatchdog();
        const bool connectedBefore = client.connected();
        ControllerState held{};
        held.buttons = XINPUT_GAMEPAD_X | XINPUT_GAMEPAD_A;
        held.leftStickX = 37; held.rightStickY = -19; held.l2 = 73;
        const auto seed = client.sendDetailed(held, true);
        client.setEnabled(false);
        const auto disabled = client.releaseSquareForWatchdog();
        const auto disabledSnapshot = client.lastSent();
        client.setEnabled(true);
        const auto result = client.releaseSquareForWatchdog();
        const auto last = client.lastSent();
        const bool connectedAfter = client.connected();
        const auto again = client.releaseSquareForWatchdog();
        client.resetConnection(); server.join();
        // Assertions occur only after join so a failed check never leaks a thread.
        QCOMPARE(before.attempted, 0);
        QVERIFY(!connectedBefore);
        QCOMPARE(seed, InputRouteWriteResult::LocalUdpAccepted);
        QCOMPARE(disabled.attempted, 0);
        QVERIFY((disabledSnapshot.buttons & kSquare) != 0);
        QCOMPARE(result.attempted, missingAck == 1 ? 1 : 2);
        QCOMPARE(result.accepted, missingAck < 0 ? 2 : missingAck - 1);
        QCOMPARE(again.attempted, 0); // no reconnect, no third burst
        QCOMPARE(connectedAfter, missingAck < 0);
        QCOMPARE(error.load(), DWORD{ERROR_SUCCESS});
        QCOMPARE(packets.size(), size_t(missingAck == 1 ? 2 : 3));
        for (size_t i = 1; i < packets.size(); ++i) {
            QCOMPARE(packets[i].buttons, packets[0].buttons & ~kSquare);
            QCOMPARE(packets[i].left_x, packets[0].left_x);
            QCOMPARE(packets[i].right_y, packets[0].right_y);
            QCOMPARE(packets[i].l2_state, packets[0].l2_state);
            QCOMPARE(packets[i].own, uint8_t{1});
            QVERIFY(packets[i].seq > packets[i - 1].seq);
            QVERIFY(hasFlag(packets[i].reserved, OrionInputMustDeliver));
            QVERIFY(hasFlag(packets[i].reserved, OrionInputShotRelease));
        }
        QCOMPARE((last.buttons & kSquare) != 0, missingAck == 1);
#else
        QSKIP("duplex named-pipe test is Windows-only");
#endif
    }


    void ownedNeutralCleanup_data()
    {
        QTest::addColumn<int>("missingAck");
        QTest::newRow("held-to-neutral-and-neutral-reassert") << -1;
        QTest::newRow("release-ack-missing") << 1;
        QTest::newRow("neutral-reassert-ack-missing") << 2;
    }
    void ownedNeutralCleanup()
    {
#ifdef _WIN32
        QFETCH(int, missingAck);
        const auto name = QByteArray("\\\\.\\pipe\\orion_cleanup_")
            + QByteArray::number(GetCurrentProcessId()) + '_'
            + QByteArray::number(GetTickCount64());
        std::promise<void> readyPromise;
        auto ready = readyPromise.get_future();
        std::atomic<DWORD> error{ERROR_SUCCESS};
        std::vector<OrionInputPacket> packets;
        std::thread server([&]() {
            HANDLE pipe = CreateNamedPipeA(name.constData(), PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                1, sizeof(OrionInputAck) * 8, sizeof(OrionInputPacket) * 8, 0, nullptr);
            if (pipe == INVALID_HANDLE_VALUE) {
                error.store(GetLastError()); readyPromise.set_value(); return;
            }
            readyPromise.set_value();
            if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
                error.store(GetLastError()); CloseHandle(pipe); return;
            }
            for (int i = 0; i < 3; ++i) {
                OrionInputPacket packet{};
                DWORD count = 0;
                if (!ReadFile(pipe, &packet, sizeof(packet), &count, nullptr)
                    || count != sizeof(packet)) { error.store(ERROR_READ_FAULT); break; }
                packets.push_back(packet);
                if (i == missingAck) { Sleep(200); break; }
                OrionInputAck ack{};
                ack.magic = kOrionInputAckMagic;
                ack.sourceSeq = packet.seq;
                ack.stage = static_cast<uint8_t>(OrionInputAckStage::LocalUdpAccepted);
                if (!WriteFile(pipe, &ack, sizeof(ack), &count, nullptr)
                    || count != sizeof(ack)) { error.store(ERROR_WRITE_FAULT); break; }
                FlushFileBuffers(pipe);
            }
            DisconnectNamedPipe(pipe); CloseHandle(pipe);
        });
        ready.wait();
        if (error.load() != ERROR_SUCCESS) {
            server.join(); QFAIL("cleanup test pipe creation failed");
        }
        OrionInputClient client(QString::fromLatin1(name));
        client.setEnabled(true);
        ControllerState held{};
        held.buttons = XINPUT_GAMEPAD_X | XINPUT_GAMEPAD_A;
        held.leftStickX = 37; held.rightStickY = -19; held.l2 = 73; held.r2 = 255;
        ControllerState neutral{};
        neutral.lightbarSet = false;
        const auto seed = client.sendDetailed(held, true);
        const auto cleanup = client.sendDetailed(neutral, true, true);
        // Do not reconnect/reseed after ambiguity. This mirrors mandatory
        // teardown's established-owned-route gate, not a retry loop.
        auto reassert = InputRouteWriteResult::Failed;
        if (client.connected() && cleanup == InputRouteWriteResult::LocalUdpAccepted)
            reassert = client.sendDetailed(neutral, true, true);
        const bool connectedAfter = client.connected();
        client.resetConnection();
        server.join();
        QCOMPARE(seed, InputRouteWriteResult::LocalUdpAccepted);
        QCOMPARE(cleanup == InputRouteWriteResult::LocalUdpAccepted, missingAck != 1);
        QCOMPARE(reassert == InputRouteWriteResult::LocalUdpAccepted, missingAck < 0);
        QCOMPARE(connectedAfter, missingAck < 0);
        QCOMPARE(error.load(), DWORD{ERROR_SUCCESS});
        QCOMPARE(packets.size(), size_t(missingAck == 1 ? 2 : 3));
        QVERIFY((packets.front().buttons & kSquare) != 0);
        for (size_t i = 1; i < packets.size(); ++i) {
            QCOMPARE(packets[i].buttons, uint32_t{0});
            QCOMPARE(packets[i].left_x, int16_t{0});
            QCOMPARE(packets[i].left_y, int16_t{0});
            QCOMPARE(packets[i].right_x, int16_t{0});
            QCOMPARE(packets[i].right_y, int16_t{0});
            QCOMPARE(packets[i].l2_state, uint8_t{0});
            QCOMPARE(packets[i].r2_state, uint8_t{0});
            QCOMPARE(packets[i].own, uint8_t{1});
            QVERIFY(packets[i].seq > packets[i-1].seq);
            QVERIFY(hasFlag(packets[i].reserved, OrionInputMustDeliver));
            QCOMPARE(hasFlag(packets[i].reserved, OrionInputShotRelease), i == 1);
        }
#else
        QSKIP("duplex named-pipe test is Windows-only");
#endif
    }

    void squareWatchdogRequiresConfirmedUnownedHold()
    {
        SquareOutputWatchdog w;
        QVERIFY(!w.releaseDue(10000, false));
        w.observeOutput(true, 100);
        QVERIFY(!w.releaseDue(1599, false));
        QVERIFY(w.releaseDue(1600, false));
        // Active shot and intentional abort drain both mean owned=true.
        QVERIFY(!w.releaseDue(90000, true));
        w.observeOutput(false, 90001);
        QVERIFY(!w.releaseDue(100000, false));
        w.observeOutput(true, 100001);
        QVERIFY(!w.releaseDue(101500, false));
        QVERIFY(w.releaseDue(101501, false));
    }
    void squareWatchdogRequiresThreeRealUpsBeforeFreshHumanHold()
    {
        SquareOutputWatchdog w;
        w.noteReleaseAttempt();
        for (int i = 0; i < 10; ++i) w.observePhysical(false, false);
        QVERIFY(w.suppressSquare()); // absence is not human release
        w.observePhysical(true, false);
        w.observePhysical(true, false);
        QVERIFY(w.suppressSquare());
        w.observePhysical(true, true); // a human held Square restarts debounce
        w.observePhysical(true, false);
        w.observePhysical(true, false);
        QVERIFY(w.suppressSquare());
        w.observePhysical(true, false);
        QVERIFY(!w.suppressSquare());
        w.observeOutput(true, 2000);
        QVERIFY(!w.releaseDue(3499, false));
        QVERIFY(w.releaseDue(3500, false));
    }
    void squareWatchdogCannotBurstOrInheritClockRegression()
    {
        SquareOutputWatchdog w;
        w.observeOutput(true, 10000);
        w.observeOutput(true, 50);
        QVERIFY(!w.releaseDue(1549, false));
        QVERIFY(w.releaseDue(1550, false));
        w.noteReleaseAttempt();
        w.observeOutput(true, 1600);
        QVERIFY(!w.releaseDue(90000, false));
    }

    void squareUpAuditDistinguishesRawAndDebouncedWithoutFlooding()
    {
        SquareUpAuditTracker audit;
        for (int i = 0; i < 20; ++i)
            QCOMPARE(audit.observe(false), SquareUpAuditPhase::None);
        for (int i = 0; i < 20; ++i)
            QCOMPARE(audit.observe(true), SquareUpAuditPhase::None);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::RawUp);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::None);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::DebouncedUp);
        for (int i = 0; i < 20; ++i)
            QCOMPARE(audit.observe(false), SquareUpAuditPhase::None);
        QCOMPARE(audit.observe(true), SquareUpAuditPhase::None);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::RawUp);
    }

    void squareUpAuditDoesNotPromoteBlipsOrRouteAbsence()
    {
        SquareUpAuditTracker audit;
        (void)audit.observe(true);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::RawUp);
        QCOMPARE(audit.observe(true), SquareUpAuditPhase::None);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::RawUp);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::None);
        QCOMPARE(audit.observe(true), SquareUpAuditPhase::None);
        audit.reset(); // selected device absent: no fabricated release event
        for (int i = 0; i < 20; ++i)
            QCOMPARE(audit.observe(false), SquareUpAuditPhase::None);
        (void)audit.observe(true);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::RawUp);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::None);
        QCOMPARE(audit.observe(false), SquareUpAuditPhase::DebouncedUp);
    }

    void latencyCalibrationSetupPromptIsExplicitAndSessionScoped()
    {
        LatencyCalibrationSetupPolicy policy;
        QVERIFY(!policy.claimPrompt(true, false, false)); // no Running session

        policy.onSessionRunning(true);
        QVERIFY(!policy.claimPrompt(false, false, false)); // wait for the complete route
        QVERIFY(!policy.claimPrompt(true, true, false));   // ready routes need no setup
        QVERIFY(policy.claimPrompt(true, false, false));
        QVERIFY(policy.promptClaimed());
        QVERIFY(!policy.claimPrompt(true, false, true)); // active explicit setup needs no prompt

        policy.suppressForCurrentSession();
        QVERIFY(policy.suppressed());
        QVERIFY(!policy.claimPrompt(true, false, false));

        policy.onSessionRunning(false);
        policy.onSessionRunning(true);
        QVERIFY(!policy.suppressed());
        QVERIFY(!policy.promptClaimed());
        QVERIFY(policy.claimPrompt(true, false, false)); // new session, one fresh prompt
    }

    void latencyCalibrationCancelBeforeRouteSuppressesPrompt()
    {
        LatencyCalibrationSetupPolicy policy;
        policy.onSessionRunning(true);
        policy.suppressForCurrentSession();
        QVERIFY(!policy.claimPrompt(true, false, false));
        QVERIFY(policy.suppressed());
    }

    void latencyCalibrationPromptReopensOnRouteOrAuthorityEpoch()
    {
        LatencyCalibrationSetupPolicy policy;
        policy.onSessionRunning(true);
        QVERIFY(policy.claimPrompt(true, false, false));
        QVERIFY(!policy.claimPrompt(true, false, true));

        // Controller/input route disappears and recovers while Remote Play remains Running.
        QVERIFY(!policy.claimPrompt(false, false, false));
        QVERIFY(policy.claimPrompt(true, false, false));
        QVERIFY(!policy.claimPrompt(true, false, true));

        // A converged estimator epoch later disappears (sidecar/source restart) without a route
        // transition. That is one new explicit-setup prompt, never controller authority.
        QVERIFY(!policy.claimPrompt(true, true, false));
        QVERIFY(policy.claimPrompt(true, false, false));

        // An involuntary engine reset can stop explicit setup without changing either high-level
        // truth. The UI may remind the user, but it still cannot restart setup itself.
        QVERIFY(!policy.claimPrompt(true, false, true));
        QVERIFY(policy.claimPrompt(true, false, false));

        // Manual cancel remains stronger than either recovery edge.
        policy.suppressForCurrentSession();
        QVERIFY(!policy.claimPrompt(false, false, false));
        QVERIFY(!policy.claimPrompt(true, false, false));
        QVERIFY(!policy.claimPrompt(true, true, false));
        QVERIFY(!policy.claimPrompt(true, false, false));
    }

    void releaseMarkerRequiresMatchingSuccessfulDelivery()
    {
        ReleaseMarkerDeliveryGate gate;
        constexpr double intentEpoch = 1'800'000'000'000.0;
        constexpr double acceptedEpoch = intentEpoch + 5.0;

        QVERIFY(gate.stage(41, intentEpoch, true));
        QVERIFY(!gate.resolve(41, false, acceptedEpoch, false).has_value());
        QVERIFY(!gate.pending());

        QVERIFY(gate.stage(42, intentEpoch, false));
        QVERIFY(!gate.resolve(99, true, acceptedEpoch, false).has_value());
        QVERIFY(!gate.pending());

        QVERIFY(gate.stage(43, intentEpoch, false));
        const auto ordinary = gate.resolve(43, true, acceptedEpoch, false);
        QVERIFY(ordinary.has_value());
        QCOMPARE(ordinary->seq, 43);
        QCOMPARE(ordinary->wallMsEpoch, acceptedEpoch);
        QVERIFY(!ordinary->latencyCalibration);
        QCOMPARE(ordinary->validationTargetPct, -1.0);
        QCOMPARE(ordinary->validationTolerancePct, -1.0);
        QVERIFY(!gate.resolve(43, true, acceptedEpoch, false).has_value());

        // A precise worker already captured the physical-submit time. Delivery confirmation
        // preserves that backdated timestamp instead of replacing it with GUI catch-up time.
        QVERIFY(gate.stage(44, intentEpoch, true));
        const auto precise = gate.resolve(44, true, acceptedEpoch, true);
        QVERIFY(precise.has_value());
        QCOMPARE(precise->wallMsEpoch, intentEpoch);
        QVERIFY(precise->latencyCalibration);

        QVERIFY(gate.stage(45, intentEpoch, true, 88.0, 3.5, 701, 801));
        const auto validation = gate.resolve(45, true, acceptedEpoch, true);
        QVERIFY(validation.has_value());
        QCOMPARE(validation->validationTargetPct, 88.0);
        QCOMPARE(validation->validationTolerancePct, 3.5);
        QCOMPARE(validation->physicalShotEpoch, quint64(701));
        QCOMPARE(validation->shotAttempt, quint64(801));
        QVERIFY(!gate.stage(46, intentEpoch, false, 88.0, 3.5));
        QVERIFY(!gate.stage(47, intentEpoch, true, 88.0, -1.0));
        QVERIFY(!gate.stage(48, intentEpoch, false, -1.0, -1.0, 701, 0));
    }

    void wireLayoutStaysCompatible()
    {
        QCOMPARE(sizeof(OrionInputPacket), size_t{24});
        QCOMPARE(sizeof(OrionInputAck), size_t{12});
        QCOMPARE(static_cast<int>(OrionInputMustDeliver), 1);
        QCOMPARE(static_cast<int>(OrionInputShotRelease), 2);
        QCOMPARE(static_cast<int>(OrionInputRedundantFlick), 4);
    }

    void reconnectSeedIsUrgent()
    {
        const OrionInputPacket current = ownedIdle();
        const uint8_t flags = classifyOrionInputPacketFlags(nullptr, current);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
        QVERIFY(!hasFlag(flags, OrionInputShotRelease));
    }

    void ordinaryMovementRemainsLatestWins()
    {
        OrionInputPacket previous = ownedIdle();
        OrionInputPacket current = previous;
        current.left_x = 16000;
        current.left_y = -9000;
        current.right_x = 5000;
        current.right_y = 7000;

        QCOMPARE(classifyOrionInputPacketFlags(&previous, current), uint8_t{0});
    }

    void squarePressAndReleaseAreOrderedEdges()
    {
        OrionInputPacket idle = ownedIdle();
        OrionInputPacket held = idle;
        held.buttons = kSquare;
        uint8_t flags = classifyOrionInputPacketFlags(&idle, held);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
        QVERIFY(!hasFlag(flags, OrionInputShotRelease));

        OrionInputPacket released = held;
        released.buttons = 0;
        flags = classifyOrionInputPacketFlags(&held, released);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
        QVERIFY(hasFlag(flags, OrionInputShotRelease));
        QVERIFY(!hasFlag(flags, OrionInputRedundantFlick));
    }

    void unrelatedButtonAndTriggerReleaseAreNotShotRelease()
    {
        OrionInputPacket held = ownedIdle();
        held.buttons = 1u << 0; // Cross
        held.r2_state = 255;
        OrionInputPacket released = held;
        released.buttons = 0;
        released.r2_state = 0;

        const uint8_t flags = classifyOrionInputPacketFlags(&held, released);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
        QVERIFY(!hasFlag(flags, OrionInputShotRelease));
    }

    void tempoGatherFlickAndNeutralAreOrderedEdges()
    {
        OrionInputPacket idle = ownedIdle();
        OrionInputPacket gather = idle;
        gather.right_y = 32766;
        uint8_t flags = classifyOrionInputPacketFlags(&idle, gather);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
        QVERIFY(!hasFlag(flags, OrionInputShotRelease));

        OrionInputPacket heldMovement = gather;
        heldMovement.left_x = -22000;
        QCOMPARE(classifyOrionInputPacketFlags(&gather, heldMovement), uint8_t{0});

        OrionInputPacket flick = heldMovement;
        flick.right_y = -32766;
        flags = classifyOrionInputPacketFlags(&heldMovement, flick);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
        QVERIFY(hasFlag(flags, OrionInputShotRelease));
        QVERIFY(hasFlag(flags, OrionInputRedundantFlick));

        OrionInputPacket neutral = flick;
        neutral.right_y = 0;
        flags = classifyOrionInputPacketFlags(&flick, neutral);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
        QVERIFY(hasFlag(flags, OrionInputShotRelease));
        QVERIFY(!hasFlag(flags, OrionInputRedundantFlick));
    }

    void gotoNeutralIsAReleaseEdge()
    {
        OrionInputPacket held = ownedIdle();
        held.right_y = -32766;
        OrionInputPacket neutral = held;
        neutral.right_y = 0;

        const uint8_t flags = classifyOrionInputPacketFlags(&held, neutral);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
        QVERIFY(hasFlag(flags, OrionInputShotRelease));
    }

    void diagonalRightStickDoesNotEnterShotBand()
    {
        OrionInputPacket previous = ownedIdle();
        OrionInputPacket current = previous;
        current.right_x = 26000;
        current.right_y = 26000;

        QCOMPARE(orionVerticalShotBand(current), 0);
        QCOMPARE(classifyOrionInputPacketFlags(&previous, current), uint8_t{0});
    }

    void ownershipFlipIsMustDeliver()
    {
        OrionInputPacket owned = ownedIdle();
        OrionInputPacket relinquished = owned;
        relinquished.own = 0;

        const uint8_t flags = classifyOrionInputPacketFlags(&owned, relinquished);
        QVERIFY(hasFlag(flags, OrionInputMustDeliver));
    }

    void acknowledgementsAreExactAndSequenceTagged()
    {
        OrionInputAck enqueued{};
        enqueued.magic = kOrionInputAckMagic;
        enqueued.sourceSeq = 41;
        enqueued.stage = static_cast<uint8_t>(OrionInputAckStage::Enqueued);
        QVERIFY(!orionInputAckAccepts(enqueued, 41, true));

        OrionInputAck accepted = enqueued;
        accepted.stage = static_cast<uint8_t>(OrionInputAckStage::LocalUdpAccepted);
        QVERIFY(orionInputAckAccepts(accepted, 41, true));
        QVERIFY(!orionInputAckAccepts(accepted, 42, true));
        QVERIFY(!orionInputAckAccepts(accepted, 41, false));

        OrionInputAck released = enqueued;
        released.stage = static_cast<uint8_t>(OrionInputAckStage::OwnershipReleased);
        QVERIFY(orionInputAckAccepts(released, 41, false));
        QVERIFY(!orionInputAckAccepts(released, 41, true));

        OrionInputAck late = accepted;
        late.sourceSeq = 40;
        QVERIFY2(!orionInputAckAccepts(late, 41, true),
                 "a late ACK from the prior write must never confirm the next release");
    }

    void runningTelemetryLossRequestsOnlyOneFailClosedRecovery()
    {
        QVERIFY(shouldAutoRecoverInputFromTelemetry(
            /*running=*/true, /*recoveryPending=*/false,
            /*hasExplicitInputReady=*/true, /*inputReady=*/false));
        QVERIFY(shouldAutoRecoverInputFromTelemetry(
            /*running=*/true, /*recoveryPending=*/false,
            /*hasExplicitInputReady=*/false, /*inputReady=*/false));

        // recoverInputLink() sets pending and Connecting synchronously before
        // another telemetry record can be dispatched, fencing duplicate child
        // creation and all automation authority.
        QVERIFY(!shouldAutoRecoverInputFromTelemetry(
            /*running=*/false, /*recoveryPending=*/true,
            /*hasExplicitInputReady=*/true, /*inputReady=*/false));
        QVERIFY(!shouldAutoRecoverInputFromTelemetry(
            /*running=*/true, /*recoveryPending=*/true,
            /*hasExplicitInputReady=*/true, /*inputReady=*/false));
        QVERIFY(!shouldAutoRecoverInputFromTelemetry(
            /*running=*/true, /*recoveryPending=*/false,
            /*hasExplicitInputReady=*/true, /*inputReady=*/true));
    }

    void duplexAckTransactionsStayOrderedUnderConcurrentMustDeliverStress()
    {
#ifdef _WIN32
        constexpr int kOwnedTransactions = 2500;
        constexpr int kTotalTransactions = kOwnedTransactions + 1; // final ownership release
        const QByteArray pipeName = QByteArrayLiteral("\\\\.\\pipe\\orion_input_stress_")
            + QByteArray::number(GetCurrentProcessId()) + '_'
            + QByteArray::number(GetTickCount64());

        std::promise<void> serverReadyPromise;
        std::future<void> serverReady = serverReadyPromise.get_future();
        std::atomic<DWORD> serverError{ERROR_SUCCESS};
        std::atomic<int> serverReceived{0};

        std::thread server([&]() {
            HANDLE pipe = CreateNamedPipeA(
                pipeName.constData(), PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                1, sizeof(OrionInputAck) * 64, sizeof(OrionInputPacket) * 64,
                0, nullptr);
            if(pipe == INVALID_HANDLE_VALUE)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                serverReadyPromise.set_value();
                return;
            }
            serverReadyPromise.set_value();

            const BOOL connectOk = ConnectNamedPipe(pipe, nullptr);
            if(!connectOk && GetLastError() != ERROR_PIPE_CONNECTED)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                CloseHandle(pipe);
                return;
            }

            const auto writeAck = [&](uint32_t sourceSeq, OrionInputAckStage stage) {
                OrionInputAck ack{};
                ack.magic = kOrionInputAckMagic;
                ack.sourceSeq = sourceSeq;
                ack.stage = static_cast<uint8_t>(stage);
                DWORD written = 0;
                if(!WriteFile(pipe, &ack, sizeof(ack), &written, nullptr)
                    || written != sizeof(ack))
                {
                    serverError.store(GetLastError(), std::memory_order_relaxed);
                    return false;
                }
                return true;
            };

            for(int index = 0; index < kTotalTransactions; ++index)
            {
                OrionInputPacket packet{};
                DWORD read = 0;
                if(!ReadFile(pipe, &packet, sizeof(packet), &read, nullptr)
                    || read != sizeof(packet))
                {
                    serverError.store(GetLastError(), std::memory_order_relaxed);
                    break;
                }
                if(packet.magic != 0x4E49524FU
                    || packet.seq != static_cast<uint32_t>(index + 1)
                    || (packet.reserved & OrionInputMustDeliver) == 0
                    || (index < kOwnedTransactions ? packet.own != 1 : packet.own != 0))
                {
                    serverError.store(ERROR_INVALID_DATA, std::memory_order_relaxed);
                    break;
                }

                // Reproduce the live race deterministically: admission consumes most of the old
                // single 14 ms budget, while local delivery remains inside its 10 ms contract.
                // A correct two-phase client starts a fresh bounded window at ENQUEUED.
                if(index == 0)
                    Sleep(8);
                if(!writeAck(packet.seq, OrionInputAckStage::Enqueued))
                    break;
                if(index == 0)
                    Sleep(8);
                const OrionInputAckStage finalStage = packet.own
                    ? OrionInputAckStage::LocalUdpAccepted
                    : OrionInputAckStage::OwnershipReleased;
                if(!writeAck(packet.seq, finalStage))
                    break;
                serverReceived.store(index + 1, std::memory_order_relaxed);
            }

            // A test server exits immediately after the final ownership ACK,
            // unlike the long-lived production bridge. Drain that last message
            // before DisconnectNamedPipe, which is otherwise allowed to discard
            // unread buffered data and create a test-only ambiguous release.
            if(serverReceived.load(std::memory_order_relaxed) == kTotalTransactions)
                FlushFileBuffers(pipe);
            DisconnectNamedPipe(pipe);
            CloseHandle(pipe);
        });

        serverReady.wait();
        if(serverError.load(std::memory_order_relaxed) != ERROR_SUCCESS)
        {
            server.join();
            QFAIL("test named-pipe server could not be created");
        }

        OrionInputClient client(QString::fromLatin1(pipeName));
        client.setEnabled(true);
        std::atomic<int> nextTransaction{0};
        std::atomic<int> clientFailures{0};
        std::vector<std::thread> writers;
        constexpr int kWriterThreads = 4;
        writers.reserve(kWriterThreads);
        for(int threadIndex = 0; threadIndex < kWriterThreads; ++threadIndex)
        {
            writers.emplace_back([&]() {
                ControllerState state{};
                while(nextTransaction.fetch_add(1, std::memory_order_relaxed)
                      < kOwnedTransactions)
                {
                    if(client.sendDetailed(state, true, /*forceWrite=*/true)
                        != InputRouteWriteResult::LocalUdpAccepted)
                    {
                        clientFailures.fetch_add(1, std::memory_order_relaxed);
                    }
                }
            });
        }
        for(auto& writer : writers)
            writer.join();

        ControllerState neutral{};
        OrionInputTransactionTiming releaseTiming;
        const InputRouteWriteResult release = client.sendDetailed(
            neutral, false, /*forceWrite=*/true, &releaseTiming);
        server.join();

        QCOMPARE(clientFailures.load(std::memory_order_relaxed), 0);
        QCOMPARE(release, InputRouteWriteResult::OwnershipReleased);
        QCOMPARE(serverError.load(std::memory_order_relaxed), DWORD{ERROR_SUCCESS});
        QCOMPARE(serverReceived.load(std::memory_order_relaxed), kTotalTransactions);
        QCOMPARE(client.writeCount(), uint64_t{kTotalTransactions});
        QCOMPARE(client.ackFailures(), uint64_t{0});
        QCOMPARE(client.lastAckWinError(), uint32_t{ERROR_SUCCESS});
        QCOMPARE(client.lastAckStage(),
                 uint32_t{static_cast<uint8_t>(OrionInputAckStage::OwnershipReleased)});
        QVERIFY(releaseTiming.matches(client.lastAckExpectedSeq()));
        QVERIFY(releaseTiming.writeAttempted);
        QVERIFY(releaseTiming.writeSampleValid);
        QVERIFY(releaseTiming.ackAttempted);
        QVERIFY(releaseTiming.ackSampleValid);
        const OrionInputTransactionTiming lastTiming = client.lastTransactionTiming();
        QCOMPARE(lastTiming.sourceSeq, releaseTiming.sourceSeq);
        QCOMPARE(lastTiming.writeUs, releaseTiming.writeUs);
        QCOMPARE(lastTiming.ackWaitUs, releaseTiming.ackWaitUs);
#else
        QSKIP("direct-input duplex transport is Windows-only");
#endif
    }

    void missingFinalAckClosesAmbiguousRouteWithoutReplay()
    {
#ifdef _WIN32
        const QByteArray pipeName = QByteArrayLiteral("\\\\.\\pipe\\orion_input_failclosed_")
            + QByteArray::number(GetCurrentProcessId()) + '_'
            + QByteArray::number(GetTickCount64());
        std::promise<void> serverReadyPromise;
        std::future<void> serverReady = serverReadyPromise.get_future();
        std::atomic<DWORD> serverError{ERROR_SUCCESS};
        std::atomic<int> packetsReceived{0};

        std::thread server([&]() {
            HANDLE pipe = CreateNamedPipeA(
                pipeName.constData(), PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                1, sizeof(OrionInputAck) * 4, sizeof(OrionInputPacket) * 4,
                0, nullptr);
            if(pipe == INVALID_HANDLE_VALUE)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                serverReadyPromise.set_value();
                return;
            }
            serverReadyPromise.set_value();
            const BOOL connectOk = ConnectNamedPipe(pipe, nullptr);
            if(!connectOk && GetLastError() != ERROR_PIPE_CONNECTED)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                CloseHandle(pipe);
                return;
            }

            OrionInputPacket packet{};
            DWORD read = 0;
            if(!ReadFile(pipe, &packet, sizeof(packet), &read, nullptr)
                || read != sizeof(packet)
                || packet.magic != 0x4E49524FU
                || (packet.reserved & OrionInputMustDeliver) == 0)
            {
                serverError.store(ERROR_INVALID_DATA, std::memory_order_relaxed);
            }
            else
            {
                packetsReceived.store(1, std::memory_order_relaxed);
                OrionInputAck enqueued{};
                enqueued.magic = kOrionInputAckMagic;
                enqueued.sourceSeq = packet.seq;
                enqueued.stage = static_cast<uint8_t>(OrionInputAckStage::Enqueued);
                DWORD written = 0;
                if(!WriteFile(pipe, &enqueued, sizeof(enqueued), &written, nullptr)
                    || written != sizeof(enqueued))
                {
                    serverError.store(ERROR_WRITE_FAULT, std::memory_order_relaxed);
                }
                else if(!FlushFileBuffers(pipe))
                {
                    // DisconnectNamedPipe is allowed to discard unread pipe data.  The
                    // assertion below intentionally exercises "ENQUEUED observed, final
                    // delivery missing", so make that first phase an actual handshake
                    // instead of racing the server teardown against the client's read.
                    serverError.store(GetLastError(), std::memory_order_relaxed);
                }
            }
            // Deliberately disappear after queue admission. The client cannot
            // know whether local UDP accepted the packet, so it must close and
            // must never replay the sequence through either direct input or ViGEm.
            DisconnectNamedPipe(pipe);
            CloseHandle(pipe);
        });

        serverReady.wait();
        if(serverError.load(std::memory_order_relaxed) != ERROR_SUCCESS)
        {
            server.join();
            QFAIL("fail-closed test named-pipe server could not be created");
        }

        OrionInputClient client(QString::fromLatin1(pipeName));
        client.setEnabled(true);
        ControllerState state{};
        OrionInputTransactionTiming failedTiming;
        const InputRouteWriteResult result = client.sendDetailed(
            state, true, /*forceWrite=*/true, &failedTiming);
        server.join();

        QCOMPARE(result, InputRouteWriteResult::WrittenUnconfirmed);
        QCOMPARE(serverError.load(std::memory_order_relaxed), DWORD{ERROR_SUCCESS});
        QCOMPARE(packetsReceived.load(std::memory_order_relaxed), 1);
        QCOMPARE(client.writeCount(), uint64_t{1});
        QCOMPARE(client.ackFailures(), uint64_t{1});
        QVERIFY(!client.connected());
        QVERIFY(client.lastAckWinError() != ERROR_SUCCESS);
        QCOMPARE(client.lastAckSourceSeq(), uint32_t{1});
        QCOMPARE(client.lastAckExpectedSeq(), uint32_t{1});
        QCOMPARE(client.lastAckStage(),
                 uint32_t{static_cast<uint8_t>(OrionInputAckStage::Enqueued)});
        QVERIFY(failedTiming.matches(1));
        QVERIFY(failedTiming.writeAttempted);
        QVERIFY(failedTiming.writeSampleValid);
        QVERIFY(failedTiming.ackAttempted);
        QVERIFY(failedTiming.ackSampleValid);
#else
        QSKIP("direct-input duplex transport is Windows-only");
#endif
    }

    void ambiguousWritesKeepDirectOwnershipMasked()
    {
        QVERIFY(inputRouteMayStillOwnInput(InputRouteWriteResult::WrittenUnconfirmed));
        QVERIFY(inputRouteMayStillOwnInput(InputRouteWriteResult::LocalUdpAccepted));
        QVERIFY(!inputRouteMayStillOwnInput(InputRouteWriteResult::Failed));
        QVERIFY(!inputRouteMayStillOwnInput(InputRouteWriteResult::OwnershipReleased));
        QCOMPARE(inputRouteWriteFailureResult(false, true),
                 InputRouteWriteResult::WrittenUnconfirmed);
        QCOMPARE(inputRouteWriteFailureResult(true, false),
                 InputRouteWriteResult::WrittenUnconfirmed);
        QCOMPARE(inputRouteWriteFailureResult(false, false),
                 InputRouteWriteResult::Failed);
    }

    void preciseFireArmRequiresDispatchRunway()
    {
        using P = PreciseFirePolicy;

        // An expired deadline used to be normalised to delay=0 and authorised,
        // turning a late mailbox handoff into an immediate (and necessarily
        // late) release.  The complete 3 ms direct-submit budget is reserved.
        QVERIFY(!P::evaluate(100.0, -1.0, 100.0).allowed);
        QVERIFY(!P::evaluate(99.0, -1.0, 100.0).allowed);
        QVERIFY(!P::evaluate(102.999, -1.0, 100.0).allowed);
        QVERIFY(!P::evaluate(103.0, -1.0, 100.0).allowed);

        const PreciseFireArmWindow viable = P::evaluate(103.001, -1.0, 100.0);
        QVERIFY(viable.allowed);
        QVERIFY(!viable.finiteAuthority);
        QVERIFY(std::abs(viable.delayMs - 3.001) < 1e-9);

        // Finite authority does not weaken the same deadline guard, and must
        // still cover the eventual fire target after admission.
        QVERIFY(!P::evaluate(103.0, 110.0, 100.0).allowed);
        QVERIFY(!P::evaluate(104.0, 103.5, 100.0).allowed);
        const PreciseFireArmWindow finite = P::evaluate(104.0, 110.0, 100.0);
        QVERIFY(finite.allowed);
        QVERIFY(finite.finiteAuthority);
        QCOMPARE(finite.delayMs, 4.0);
        QCOMPARE(finite.authorityRemainingMs, 10.0);

        QVERIFY(!P::evaluate(
                     std::numeric_limits<double>::max(), -1.0,
                     -std::numeric_limits<double>::max()).allowed);
    }

    void elapsedClockSamplesRejectFailureAndRegression()
    {
        const CheckedElapsedMicros exact = checkedElapsedMicros(
            true, true, 1'000, 26'000, 10'000'000);
        QVERIFY(exact.valid);
        QCOMPARE(exact.value, std::uint64_t{2'500});

        const CheckedElapsedMicros zero = checkedElapsedMicros(
            true, true, 42, 42, 10'000'000);
        QVERIFY(zero.valid);
        QCOMPARE(zero.value, std::uint64_t{0});

        QVERIFY(!checkedElapsedMicros(false, true, 100, 200, 10'000'000).valid);
        QVERIFY(!checkedElapsedMicros(true, false, 100, 200, 10'000'000).valid);
        QVERIFY(!checkedElapsedMicros(true, true, 100, 200, 0).valid);
        QVERIFY(!checkedElapsedMicros(true, true, 100, 200, -1).valid);
        QVERIFY2(!checkedElapsedMicros(true, true, 200, 100, 10'000'000).valid,
                 "a backward QPC sample must never wrap into uint64 telemetry");
        QVERIFY2(!checkedElapsedMicros(
                     true, true,
                     std::numeric_limits<std::int64_t>::min(),
                     std::numeric_limits<std::int64_t>::max(), 1).valid,
                 "an unrepresentable microsecond conversion must fail instead of overflowing");

        // Signed ordering remains sound on the negative side of the synthetic
        // domain as well; the unsigned subtraction happens only after this gate.
        const CheckedElapsedMicros signedDomain = checkedElapsedMicros(
            true, true, -200, -100, 1'000'000);
        QVERIFY(signedDomain.valid);
        QCOMPARE(signedDomain.value, std::uint64_t{100});

        // The whole-second product may fit while adding the fractional part
        // does not.  Reject rather than wrap that duration into a tiny sample.
        QVERIFY2(!checkedElapsedMicros(
                     true, true,
                     std::numeric_limits<std::int64_t>::min(),
                     9'223'353'590'111'150'481LL,
                     999'999).valid,
                 "the fractional-microsecond addition must also be overflow checked");
    }

    void transactionTimingCannotInheritPriorSamples()
    {
        OrionInputTransactionTiming timing;
        timing.begin(41);
        timing.recordWrite({true, 17});
        timing.beginAck();
        timing.recordAck({true, 233});
        QVERIFY(timing.matches(41));
        QVERIFY(timing.writeAttempted);
        QVERIFY(timing.writeSampleValid);
        QCOMPARE(timing.writeUs, std::uint64_t{17});
        QVERIFY(timing.ackAttempted);
        QVERIFY(timing.ackSampleValid);
        QCOMPARE(timing.ackWaitUs, std::uint64_t{233});

        // A new transaction with failed/regressing clock samples must carry
        // its own sequence and explicit invalidity.  It may never inherit the
        // 17/233 us values from transaction 41.
        timing.begin(42);
        timing.recordWrite({false, std::numeric_limits<std::uint64_t>::max()});
        timing.beginAck();
        timing.recordAck({false, std::numeric_limits<std::uint64_t>::max()});
        QVERIFY(!timing.matches(41));
        QVERIFY(timing.matches(42));
        QVERIFY(timing.writeAttempted);
        QVERIFY(!timing.writeSampleValid);
        QCOMPARE(timing.writeUs, std::uint64_t{0});
        QVERIFY(timing.ackAttempted);
        QVERIFY(!timing.ackSampleValid);
        QCOMPARE(timing.ackWaitUs, std::uint64_t{0});

        // Starting transaction 43 also clears ACK-attempt state, so an
        // ordinary no-ACK packet cannot be mislabeled with transaction 42.
        timing.begin(43);
        timing.recordWrite({true, 0});
        QVERIFY(timing.matches(43));
        QVERIFY(timing.writeSampleValid);
        QCOMPARE(timing.writeUs, std::uint64_t{0});
        QVERIFY(!timing.ackAttempted);
        QVERIFY(!timing.ackSampleValid);
        QCOMPARE(timing.ackWaitUs, std::uint64_t{0});
    }

    void deliveryStageFieldsStayParserStable()
    {
        QVERIFY(!PreciseFirePolicy::requiresExactDeliveryAck(
            false, false, false, false));
        QVERIFY(PreciseFirePolicy::requiresExactDeliveryAck(
            true, false, false, false));
        QVERIFY(!PreciseFirePolicy::requiresExactDeliveryAck(
            true, true, false, false));
        QVERIFY(PreciseFirePolicy::requiresExactDeliveryAck(
            false, false, true, false));
        QVERIFY(PreciseFirePolicy::requiresExactDeliveryAck(
            true, true, true, false));
        // A new physical Square epoch is itself an exact transaction. It must
        // survive both the ordinary mirror coalescer and a just-delivered
        // release's duplicate-suppression window.
        QVERIFY(PreciseFirePolicy::requiresExactDeliveryAck(
            false, false, false, true));
        QVERIFY(PreciseFirePolicy::requiresExactDeliveryAck(
            true, true, false, true));

        QVERIFY(PreciseFirePolicy::hasConfirmedPreciseDelivery(
            PreciseFireDeliveryStage::LocalUdpAccepted, 41));
        QVERIFY(PreciseFirePolicy::hasConfirmedPreciseDelivery(
            PreciseFireDeliveryStage::ActiveVigemSubmit, 42));
        QVERIFY(!PreciseFirePolicy::hasConfirmedPreciseDelivery(
            PreciseFireDeliveryStage::None, 43));
        QVERIFY(!PreciseFirePolicy::hasConfirmedPreciseDelivery(
            PreciseFireDeliveryStage::LocalUdpAccepted, 0));

        // A pending release must never disappear inside the ordinary 3 ms
        // mirror coalescing window. Non-critical frames retain coalescing.
        QVERIFY(!PreciseFirePolicy::shouldAttemptDirectWrite(
            false, true, false, 0, 3000));
        QVERIFY(PreciseFirePolicy::shouldAttemptDirectWrite(
            true, true, false, 0, 3000));
        QVERIFY(!PreciseFirePolicy::shouldAttemptDirectWrite(
            true, false, false, 2999, 3000));
        QVERIFY(PreciseFirePolicy::shouldAttemptDirectWrite(
            true, false, false, 3000, 3000));
        // Both direct-pipe and ViGEm worker confirmations suppress the GUI's
        // same-release route write, even outside the ordinary coalesce window.
        QVERIFY(!PreciseFirePolicy::shouldAttemptDirectWrite(
            true, false, true, 10'000, 3000));
        // A forced route-recovery ACK is stronger than duplicate suppression.
        QVERIFY(PreciseFirePolicy::shouldAttemptDirectWrite(
            true, true, true, 0, 3000));
        const bool squareDownExact = PreciseFirePolicy::requiresExactDeliveryAck(
            false, false, false, true);
        QVERIFY2(PreciseFirePolicy::shouldAttemptDirectWrite(
                     true, squareDownExact, true, 0, 3000),
                 "a physical Square epoch must not disappear inside coalescing or "
                 "a prior release's duplicate-suppression window");

        QCOMPARE(QString::fromLatin1(preciseFireDeliveryStageField(
                     PreciseFireDeliveryStage::LocalUdpAccepted)),
                 QStringLiteral("local_udp_accepted"));
        QCOMPARE(QString::fromLatin1(preciseFireDeliveryStageField(
                     PreciseFireDeliveryStage::ActiveVigemSubmit)),
                 QStringLiteral("active_vigem_submit"));
        QCOMPARE(QString::fromLatin1(preciseFireDeliveryStageField(
                     PreciseFireDeliveryStage::None)),
                 QStringLiteral("not_confirmed"));

        QCOMPARE(PreciseFirePolicy::evaluateDelivery(
                     InputRouteWriteResult::LocalUdpAccepted, true, false).stage,
                 PreciseFireDeliveryStage::LocalUdpAccepted);
        QCOMPARE(PreciseFirePolicy::evaluateDelivery(
                     InputRouteWriteResult::Failed, false, true).stage,
                 PreciseFireDeliveryStage::ActiveVigemSubmit);
        QCOMPARE(PreciseFirePolicy::evaluateDelivery(
                     InputRouteWriteResult::WrittenUnconfirmed, true, true).stage,
                 PreciseFireDeliveryStage::None);
    }

    void liveMeterMetricExpiresWithoutAnotherFrame()
    {
        constexpr std::int64_t observedAtMs = 1'000;
        constexpr std::int64_t freshnessMs = 120;
        QVERIFY(live_hud::meterMetricFresh(
            true, observedAtMs, observedAtMs + 119, freshnessMs));
        QVERIFY(!live_hud::meterMetricFresh(
            true, observedAtMs, observedAtMs + 120, freshnessMs));
        QVERIFY(!live_hud::meterMetricFresh(
            true, observedAtMs, observedAtMs - 1, freshnessMs));
    }

    void botOwnershipDurationUsesTakeoverT1ToReleaseT2()
    {
        constexpr std::uint64_t armToken = 17;
        constexpr double physicalPressMs = 100.0;
        constexpr double takeoverT1Ms = 250.0;
        constexpr double releaseT2Ms = 390.0;
        Q_UNUSED(physicalPressMs);

        QCOMPARE(live_hud::botOwnedDurationMs(
                     armToken, armToken, takeoverT1Ms, -1.0, 340.0),
                 90.0);
        QCOMPARE(live_hud::botOwnedDurationMs(
                     armToken, armToken, takeoverT1Ms, releaseT2Ms, 500.0),
                 140.0);
        QCOMPARE(live_hud::botOwnedDurationMs(
                     armToken + 1, armToken, takeoverT1Ms, releaseT2Ms, 500.0),
                 -1.0);
        QCOMPARE(live_hud::botOwnedDurationMs(
                     armToken, armToken, takeoverT1Ms,
                     std::numeric_limits<double>::quiet_NaN(), 500.0),
                 -1.0);
    }

    void targetEtaRequiresMatchingActiveTargetAndSubtractsAge()
    {
        constexpr std::int64_t observedAtMs = 2'000;
        constexpr std::int64_t nowMs = 2'040;
        constexpr std::int64_t freshnessMs = 120;
        constexpr std::uint64_t armToken = 9;

        QCOMPARE(live_hud::matchingActiveTargetEtaMs(
                     true, observedAtMs, nowMs, freshnessMs,
                     armToken, armToken, 99.5, 99.5, 105.0),
                 65.0);
        // A green-centre ETA cannot be displayed under a tip-target label.
        QCOMPARE(live_hud::matchingActiveTargetEtaMs(
                     true, observedAtMs, nowMs, freshnessMs,
                     armToken, armToken, 99.5, 95.0, 105.0),
                 -1.0);
        // Nor can a prior shot's target crossing survive an arm-token change.
        QCOMPARE(live_hud::matchingActiveTargetEtaMs(
                     true, observedAtMs, nowMs, freshnessMs,
                     armToken, armToken - 1, 99.5, 99.5, 105.0),
                 -1.0);
    }

    void sidecarInfoRelayIsShotScopedOnly()
    {
        QVERIFY(shouldRelaySidecarInfoLine(QByteArrayLiteral(
            "2026-08-01 01:05:48,987 INFO RemotePlayOrchestrator: release marker: "
            "seq=14 wall_ms=1785564348987 rtt_ms=0.0 measured_latency_ms=0.0")));
        QVERIFY(shouldRelaySidecarInfoLine(QByteArrayLiteral(
            "2026-08-01 01:05:49,200 INFO latency_estimator: latency observation: "
            "seq=14 calibration=1 accepted=0 status=rejected_validation_stop_residual")));

        QVERIFY(!shouldRelaySidecarInfoLine(QByteArrayLiteral(
            "2026-08-01 01:05:48,987 INFO RemotePlayOrchestrator: RTT tick: raw=0.6ms")));
        QVERIFY(!shouldRelaySidecarInfoLine(QByteArrayLiteral(
            "2026-08-01 01:05:48,987 WARNING RemotePlayOrchestrator: release marker: seq=14")));
        QVERIFY(!shouldRelaySidecarInfoLine(QByteArrayLiteral(
            "2026-08-01 01:05:48,987 INFO OtherLogger: release marker: seq=14")));
        QVERIFY(!shouldRelaySidecarInfoLine(QByteArrayLiteral(
            "2026-08-01 01:05:49,200 INFO OtherLogger: latency observation: seq=14")));
        QVERIFY(!shouldRelaySidecarInfoLine(QByteArrayLiteral(
            "2026-08-01 01:05:48,987 INFO RemotePlayOrchestrator: release marker failed")));
    }

    // [DPAD-UP BYPASS HOTKEY 2026-08-08] Edge-trigger semantics of the physical
    // D-pad Up meter-delay bypass toggle, driven through the same ControllerState
    // button mask the production poll unpacks (XINPUT_GAMEPAD_DPAD_UP = 0x0001,
    // set by xinputDpadBitsFromNibble from the DualSense HID D-pad nibble).
    void dpadUpBypassHotkeyIsStrictlyEdgeTriggered()
    {
        DpadUpEdgeTracker tracker;
        ControllerState state{};
        const auto step = [&](uint16_t buttons, bool connected) {
            state.buttons = buttons;
            return tracker.update(
                (state.buttons & XINPUT_GAMEPAD_DPAD_UP) != 0, connected);
        };

        // Press -> exactly one toggle edge.
        QVERIFY(step(XINPUT_GAMEPAD_DPAD_UP, true));
        // Hold -> no further toggles, tick after tick.
        QVERIFY(!step(XINPUT_GAMEPAD_DPAD_UP, true));
        QVERIFY(!step(XINPUT_GAMEPAD_DPAD_UP, true));
        // Square (0x4000) alongside the hold changes nothing: the hotkey reads
        // only the D-pad Up bit and never interferes with shot input.
        QVERIFY(!step(XINPUT_GAMEPAD_DPAD_UP | XINPUT_GAMEPAD_X, true));
        // Release -> nothing fires, through the full debounce lifetime.
        for (int i = 0; i < DpadUpEdgeTracker::kReleaseSamples; ++i) {
            QVERIFY(!step(0, true));
        }
        // Press again -> toggles back with exactly one edge.
        QVERIFY(step(XINPUT_GAMEPAD_DPAD_UP, true));
        QVERIFY(!step(XINPUT_GAMEPAD_DPAD_UP, true));

        // A single false-UP poll mid-hold (transport blip) must not re-toggle:
        // the latch shares the shot-intent kReleaseSamples release debounce.
        QVERIFY(!step(0, true));
        QVERIFY(!step(XINPUT_GAMEPAD_DPAD_UP, true));

        // Other buttons alone never trigger the hotkey.
        for (int i = 0; i < DpadUpEdgeTracker::kReleaseSamples; ++i) {
            QVERIFY(!step(0, true));
        }
        QVERIFY(!step(XINPUT_GAMEPAD_X, true));
        QVERIFY(!step(XINPUT_GAMEPAD_DPAD_DOWN, true));
    }

    void dpadUpBypassHotkeyInertOutsideConnectedSession()
    {
        DpadUpEdgeTracker tracker;
        // Pressed while idle / in the launcher -> inert.
        QVERIFY(!tracker.update(true, false));
        // Still held when the session connects -> the stale press cannot fire.
        QVERIFY(!tracker.update(true, true));
        // Release, then a fresh in-session press -> exactly one toggle.
        for (int i = 0; i < DpadUpEdgeTracker::kReleaseSamples; ++i) {
            QVERIFY(!tracker.update(false, true));
        }
        QVERIFY(tracker.update(true, true));
        // Disconnect mid-hold and reconnect -> the old edge is never replayed.
        QVERIFY(!tracker.update(true, false));
        QVERIFY(!tracker.update(true, true));
    }

    // [PRESSED OVERLAY 2026-08-08] The local "PRESSED" acknowledgment badge is
    // backed by SquareHeldLatch, fed from the SAME selected-device poll tick
    // that feeds the meter-delay actuator (OrionAppController::
    // syncPressedOverlay, adjacent to setPhysicalSquareHeld and the "Physical
    // shot epoch" record). The latch must report the flip from the very
    // update() call that consumes the sample — synchronous, never deferred to a
    // later tick, timer, or heartbeat — so the badge's latency guarantee is the
    // latch's edge semantics.
    void pressedOverlaySquareLatchFlipsOnTheSameTick()
    {
        SquareHeldLatch latch;
        ControllerState state{};
        QVERIFY(!latch.held());

        // Square-down (raw_button_mask 0x4000): the SAME update() call reports
        // the change AND the held state already reads true.
        state.buttons = XINPUT_GAMEPAD_X;
        QVERIFY(latch.update(state.square()));
        QVERIFY(latch.held());

        // Held: no re-notification tick after tick.
        QVERIFY(!latch.update(state.square()));
        QVERIFY(!latch.update(state.square()));
        QVERIFY(latch.held());

        // Square-up: flips false on the same call, again exactly once.
        state.buttons = 0;
        QVERIFY(latch.update(state.square()));
        QVERIFY(!latch.held());
        QVERIFY(!latch.update(state.square()));

        // Other buttons never light the badge — it reads the raw Square bit
        // only (Cross + D-pad Up alongside change nothing).
        state.buttons = XINPUT_GAMEPAD_A | XINPUT_GAMEPAD_DPAD_UP;
        QVERIFY(!latch.update(state.square()));
        QVERIFY(!latch.held());

        // Route loss mid-hold: the production poll feeds false on
        // !selected.active, so an unplugged pad cannot leave the badge lit.
        state.buttons = XINPUT_GAMEPAD_X;
        QVERIFY(latch.update(state.square()));
        QVERIFY(latch.held());
        QVERIFY(latch.update(false));
        QVERIFY(!latch.held());
    }

    // [ORION_PRESS_DELIVERY_AUDIT 2026-08-30] Regression tests for the owner invariant:
    // a physical state change must always be able to reach the console, and no
    // optimisation (de-dupe included) may ever suppress a state change the console has
    // not provably received. These would have caught the de-dupe divergence class where
    // a lost/ambiguous write leaves `last_` claiming a state the console never got.

    // A write whose delivery became AMBIGUOUS (ENQUEUED observed, final ACK missing)
    // must never poison the de-dup snapshot: after the route re-connects, sending the
    // byte-identical state again must produce a real, MustDeliver, ACK-confirmed write —
    // not InputRouteWriteResult::Unchanged.
    void dedupeCannotSuppressStateChangeAfterAmbiguousWrite()
    {
#ifdef _WIN32
        const QByteArray pipeName = QByteArrayLiteral("\\\\.\\pipe\\orion_input_dedupe_amb_")
            + QByteArray::number(GetCurrentProcessId()) + '_'
            + QByteArray::number(GetTickCount64());

        const auto writeAck = [](HANDLE pipe, uint32_t sourceSeq, OrionInputAckStage stage) {
            OrionInputAck ack{};
            ack.magic = kOrionInputAckMagic;
            ack.sourceSeq = sourceSeq;
            ack.stage = static_cast<uint8_t>(stage);
            DWORD written = 0;
            return WriteFile(pipe, &ack, sizeof(ack), &written, nullptr)
                && written == sizeof(ack);
        };

        std::promise<void> phase1ReadyPromise;
        std::future<void> phase1Ready = phase1ReadyPromise.get_future();
        std::atomic<DWORD> serverError{ERROR_SUCCESS};

        // Phase 1: full-ack the seed, then answer the press with ENQUEUED only and vanish
        // (the exact "may have been accepted" ambiguity the production bridge can create).
        std::thread phase1([&]() {
            HANDLE pipe = CreateNamedPipeA(
                pipeName.constData(), PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                1, sizeof(OrionInputAck) * 8, sizeof(OrionInputPacket) * 8, 0, nullptr);
            if(pipe == INVALID_HANDLE_VALUE)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                phase1ReadyPromise.set_value();
                return;
            }
            phase1ReadyPromise.set_value();
            const BOOL connectOk = ConnectNamedPipe(pipe, nullptr);
            if(!connectOk && GetLastError() != ERROR_PIPE_CONNECTED)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                CloseHandle(pipe);
                return;
            }
            for(int index = 0; index < 2; ++index)
            {
                OrionInputPacket packet{};
                DWORD read = 0;
                if(!ReadFile(pipe, &packet, sizeof(packet), &read, nullptr)
                    || read != sizeof(packet))
                {
                    serverError.store(ERROR_READ_FAULT, std::memory_order_relaxed);
                    break;
                }
                if(!writeAck(pipe, packet.seq, OrionInputAckStage::Enqueued))
                {
                    serverError.store(ERROR_WRITE_FAULT, std::memory_order_relaxed);
                    break;
                }
                if(index == 0)
                {
                    if(!writeAck(pipe, packet.seq, OrionInputAckStage::LocalUdpAccepted))
                    {
                        serverError.store(ERROR_WRITE_FAULT, std::memory_order_relaxed);
                        break;
                    }
                }
                else
                {
                    // The ENQUEUED for the press must actually reach the client before
                    // teardown discards it, so the ambiguity is real, not a lost write.
                    FlushFileBuffers(pipe);
                }
            }
            DisconnectNamedPipe(pipe);
            CloseHandle(pipe);
        });

        phase1Ready.wait();
        if(serverError.load(std::memory_order_relaxed) != ERROR_SUCCESS)
        {
            phase1.join();
            QFAIL("phase-1 named-pipe server could not be created");
        }

        OrionInputClient client(QString::fromLatin1(pipeName));
        client.setEnabled(true);

        ControllerState neutral{};
        QCOMPARE(client.sendDetailed(neutral, true),
                 InputRouteWriteResult::LocalUdpAccepted);

        ControllerState pressed{};
        pressed.buttons = XINPUT_GAMEPAD_X;
        QCOMPARE(client.sendDetailed(pressed, true),
                 InputRouteWriteResult::WrittenUnconfirmed);
        phase1.join();
        QCOMPARE(serverError.load(std::memory_order_relaxed), DWORD{ERROR_SUCCESS});
        QVERIFY(!client.connected());
        // The ambiguous press must NOT have advanced the de-dup snapshot.
        QVERIFY(client.haveSent());
        QCOMPARE(client.lastSent().buttons, uint32_t{0});

        // Phase 2: a healthy replacement server on the same route.
        std::promise<void> phase2ReadyPromise;
        std::future<void> phase2Ready = phase2ReadyPromise.get_future();
        std::atomic<int> phase2Received{0};
        OrionInputPacket phase2Packet{};
        std::thread phase2([&]() {
            HANDLE pipe = CreateNamedPipeA(
                pipeName.constData(), PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                1, sizeof(OrionInputAck) * 8, sizeof(OrionInputPacket) * 8, 0, nullptr);
            if(pipe == INVALID_HANDLE_VALUE)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                phase2ReadyPromise.set_value();
                return;
            }
            phase2ReadyPromise.set_value();
            const BOOL connectOk = ConnectNamedPipe(pipe, nullptr);
            if(!connectOk && GetLastError() != ERROR_PIPE_CONNECTED)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                CloseHandle(pipe);
                return;
            }
            OrionInputPacket packet{};
            DWORD read = 0;
            if(ReadFile(pipe, &packet, sizeof(packet), &read, nullptr)
                && read == sizeof(packet))
            {
                phase2Packet = packet;
                phase2Received.store(1, std::memory_order_relaxed);
                writeAck(pipe, packet.seq, OrionInputAckStage::Enqueued);
                writeAck(pipe, packet.seq, OrionInputAckStage::LocalUdpAccepted);
                FlushFileBuffers(pipe);
            }
            else
            {
                serverError.store(ERROR_READ_FAULT, std::memory_order_relaxed);
            }
            DisconnectNamedPipe(pipe);
            CloseHandle(pipe);
        });

        phase2Ready.wait();
        if(serverError.load(std::memory_order_relaxed) != ERROR_SUCCESS)
        {
            phase2.join();
            QFAIL("phase-2 named-pipe server could not be created");
        }

        // Clear the 500 ms reconnect throttle the ordinary (non-reset) failure path keeps.
        Sleep(600);

        // The byte-identical state whose delivery was ambiguous MUST be written again.
        QCOMPARE(client.sendDetailed(pressed, true),
                 InputRouteWriteResult::LocalUdpAccepted);
        phase2.join();
        QCOMPARE(serverError.load(std::memory_order_relaxed), DWORD{ERROR_SUCCESS});
        QCOMPARE(phase2Received.load(std::memory_order_relaxed), 1);
        QCOMPARE(phase2Packet.buttons, uint32_t{kSquare});
        QCOMPARE(phase2Packet.own, uint8_t{1});
        QVERIFY2((phase2Packet.reserved & OrionInputMustDeliver) != 0,
                 "the first packet on a reconnected route must be a forced full-state seed");
        const OrionInputPacket acceptedSquare = client.lastSent();
        QCOMPARE(acceptedSquare.buttons, uint32_t{kSquare});
        QCOMPARE(acceptedSquare.seq, phase2Packet.seq);
        QCOMPARE(client.lastAckExpectedSeq(), acceptedSquare.seq);
        QCOMPARE(client.lastAckSourceSeq(), acceptedSquare.seq);
        QCOMPARE(client.lastAckStage(),
                 uint32_t{static_cast<uint8_t>(OrionInputAckStage::LocalUdpAccepted)});
#else
        QSKIP("direct-input duplex transport is Windows-only");
#endif
    }

    // A route/generation transition (resetConnection(), called at every session
    // teardown/restart/connect boundary) must guarantee the first packet afterwards is
    // written even when it is byte-identical to the last confirmed pre-transition state.
    void identicalStateAfterRouteTransitionIsRewritten()
    {
#ifdef _WIN32
        const QByteArray pipeName = QByteArrayLiteral("\\\\.\\pipe\\orion_input_routechange_")
            + QByteArray::number(GetCurrentProcessId()) + '_'
            + QByteArray::number(GetTickCount64());

        const auto writeAck = [](HANDLE pipe, uint32_t sourceSeq, OrionInputAckStage stage) {
            OrionInputAck ack{};
            ack.magic = kOrionInputAckMagic;
            ack.sourceSeq = sourceSeq;
            ack.stage = static_cast<uint8_t>(stage);
            DWORD written = 0;
            return WriteFile(pipe, &ack, sizeof(ack), &written, nullptr)
                && written == sizeof(ack);
        };

        std::promise<void> serverReadyPromise;
        std::future<void> serverReady = serverReadyPromise.get_future();
        std::atomic<DWORD> serverError{ERROR_SUCCESS};
        std::atomic<int> generationsServed{0};
        OrionInputPacket secondGenerationPacket{};

        // One pipe generation per client connect: serve exactly one full-acked packet,
        // then re-listen once for the post-transition client (mirrors the production
        // OrionStream replacement the resetConnection() contract exists for).
        std::thread server([&]() {
            HANDLE pipe = CreateNamedPipeA(
                pipeName.constData(), PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                1, sizeof(OrionInputAck) * 8, sizeof(OrionInputPacket) * 8, 0, nullptr);
            if(pipe == INVALID_HANDLE_VALUE)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                serverReadyPromise.set_value();
                return;
            }
            serverReadyPromise.set_value();
            for(int generation = 0; generation < 2; ++generation)
            {
                const BOOL connectOk = ConnectNamedPipe(pipe, nullptr);
                if(!connectOk && GetLastError() != ERROR_PIPE_CONNECTED)
                {
                    serverError.store(GetLastError(), std::memory_order_relaxed);
                    break;
                }
                OrionInputPacket packet{};
                DWORD read = 0;
                if(!ReadFile(pipe, &packet, sizeof(packet), &read, nullptr)
                    || read != sizeof(packet))
                {
                    serverError.store(ERROR_READ_FAULT, std::memory_order_relaxed);
                    break;
                }
                if(!writeAck(pipe, packet.seq, OrionInputAckStage::Enqueued)
                    || !writeAck(pipe, packet.seq, OrionInputAckStage::LocalUdpAccepted))
                {
                    serverError.store(ERROR_WRITE_FAULT, std::memory_order_relaxed);
                    break;
                }
                FlushFileBuffers(pipe);
                if(generation == 1)
                    secondGenerationPacket = packet;
                generationsServed.store(generation + 1, std::memory_order_relaxed);
                if(generation == 0)
                {
                    // Wait for the client to drop generation 0 (resetConnection closes its
                    // handle -> broken-pipe read) before re-listening. Generation 1 must NOT
                    // drain: the test joins this thread while the client is still open, so a
                    // blocking read here would deadlock the run.
                    OrionInputPacket drain{};
                    DWORD drained = 0;
                    while(ReadFile(pipe, &drain, sizeof(drain), &drained, nullptr)) {}
                }
                DisconnectNamedPipe(pipe);
            }
            CloseHandle(pipe);
        });

        serverReady.wait();
        if(serverError.load(std::memory_order_relaxed) != ERROR_SUCCESS)
        {
            server.join();
            QFAIL("route-transition named-pipe server could not be created");
        }

        OrionInputClient client(QString::fromLatin1(pipeName));
        client.setEnabled(true);
        ControllerState pressed{};
        pressed.buttons = XINPUT_GAMEPAD_X;
        QCOMPARE(client.sendDetailed(pressed, true),
                 InputRouteWriteResult::LocalUdpAccepted);
        QCOMPARE(client.lastSent().buttons, uint32_t{kSquare});

        // The production route-transition boundary: teardown/restart/connect all call this.
        client.resetConnection();
        QVERIFY(!client.connected());
        QVERIFY(!client.haveSent());

        // Identical state, new generation: MUST be a real forced full-state write.
        InputRouteWriteResult result = InputRouteWriteResult::Failed;
        for(int attempt = 0; attempt < 50; ++attempt)
        {
            result = client.sendDetailed(pressed, true);
            if(result == InputRouteWriteResult::LocalUdpAccepted)
                break;
            QVERIFY2(result == InputRouteWriteResult::Failed,
                     "an identical post-transition state must never be reported Unchanged");
            Sleep(20); // server may still be between generations
        }
        server.join();
        QCOMPARE(result, InputRouteWriteResult::LocalUdpAccepted);
        QCOMPARE(serverError.load(std::memory_order_relaxed), DWORD{ERROR_SUCCESS});
        QCOMPARE(generationsServed.load(std::memory_order_relaxed), 2);
        QCOMPARE(secondGenerationPacket.buttons, uint32_t{kSquare});
        QCOMPARE(secondGenerationPacket.own, uint8_t{1});
        QVERIFY2((secondGenerationPacket.reserved & OrionInputMustDeliver) != 0,
                 "the first post-transition packet must be a forced full-state seed");
#else
        QSKIP("direct-input duplex transport is Windows-only");
#endif
    }

    // The 1 Hz idle re-assert must (a) refuse to run without an established confirmed
    // route, (b) re-prove a healthy route with a byte-identical MustDeliver transaction
    // without disturbing the de-dup snapshot, and (c) convert a wedged route into a
    // detected, closed pipe at idle instead of leaving it for the player's next press.
    void idleReassertProvesRouteAndDetectsWedgeAtIdle()
    {
#ifdef _WIN32
        const QByteArray pipeName = QByteArrayLiteral("\\\\.\\pipe\\orion_input_reassert_")
            + QByteArray::number(GetCurrentProcessId()) + '_'
            + QByteArray::number(GetTickCount64());

        const auto writeAck = [](HANDLE pipe, uint32_t sourceSeq, OrionInputAckStage stage) {
            OrionInputAck ack{};
            ack.magic = kOrionInputAckMagic;
            ack.sourceSeq = sourceSeq;
            ack.stage = static_cast<uint8_t>(stage);
            DWORD written = 0;
            return WriteFile(pipe, &ack, sizeof(ack), &written, nullptr)
                && written == sizeof(ack);
        };

        std::promise<void> serverReadyPromise;
        std::future<void> serverReady = serverReadyPromise.get_future();
        std::atomic<DWORD> serverError{ERROR_SUCCESS};
        std::atomic<int> packetsReceived{0};
        OrionInputPacket reassertPacket{};

        std::thread server([&]() {
            HANDLE pipe = CreateNamedPipeA(
                pipeName.constData(), PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                1, sizeof(OrionInputAck) * 8, sizeof(OrionInputPacket) * 8, 0, nullptr);
            if(pipe == INVALID_HANDLE_VALUE)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                serverReadyPromise.set_value();
                return;
            }
            serverReadyPromise.set_value();
            const BOOL connectOk = ConnectNamedPipe(pipe, nullptr);
            if(!connectOk && GetLastError() != ERROR_PIPE_CONNECTED)
            {
                serverError.store(GetLastError(), std::memory_order_relaxed);
                CloseHandle(pipe);
                return;
            }
            for(int index = 0; index < 3; ++index)
            {
                OrionInputPacket packet{};
                DWORD read = 0;
                if(!ReadFile(pipe, &packet, sizeof(packet), &read, nullptr)
                    || read != sizeof(packet))
                {
                    serverError.store(ERROR_READ_FAULT, std::memory_order_relaxed);
                    break;
                }
                packetsReceived.store(index + 1, std::memory_order_relaxed);
                if(index < 2)
                {
                    if(index == 1)
                        reassertPacket = packet;
                    if(!writeAck(pipe, packet.seq, OrionInputAckStage::Enqueued)
                        || !writeAck(pipe, packet.seq, OrionInputAckStage::LocalUdpAccepted))
                    {
                        serverError.store(ERROR_WRITE_FAULT, std::memory_order_relaxed);
                        break;
                    }
                    FlushFileBuffers(pipe);
                }
                else
                {
                    // WEDGE: swallow the third transaction without any ACK. The client's
                    // bounded wait must expire and fail the route closed at idle.
                    Sleep(200);
                }
            }
            DisconnectNamedPipe(pipe);
            CloseHandle(pipe);
        });

        serverReady.wait();
        if(serverError.load(std::memory_order_relaxed) != ERROR_SUCCESS)
        {
            server.join();
            QFAIL("reassert named-pipe server could not be created");
        }

        OrionInputClient client(QString::fromLatin1(pipeName));
        client.setEnabled(true);

        // (a) no established confirmed route yet -> refuse, and never connect/seed.
        QCOMPARE(client.reassertLastState(), InputRouteWriteResult::Failed);
        QVERIFY(!client.connected());

        ControllerState pressed{};
        pressed.buttons = XINPUT_GAMEPAD_X;
        QCOMPARE(client.sendDetailed(pressed, true),
                 InputRouteWriteResult::LocalUdpAccepted);
        const OrionInputPacket confirmed = client.lastSent();

        // (b) healthy route: byte-identical MustDeliver re-assert, snapshot untouched.
        QCOMPARE(client.reassertLastState(), InputRouteWriteResult::LocalUdpAccepted);
        QCOMPARE(packetsReceived.load(std::memory_order_relaxed), 2);
        QCOMPARE(reassertPacket.buttons, confirmed.buttons);
        QCOMPARE(reassertPacket.left_x, confirmed.left_x);
        QCOMPARE(reassertPacket.right_y, confirmed.right_y);
        QCOMPARE(reassertPacket.own, uint8_t{1});
        QVERIFY((reassertPacket.reserved & OrionInputMustDeliver) != 0);
        QVERIFY2(reassertPacket.seq > confirmed.seq,
                 "a re-assert is a fresh transaction, never a stale-seq replay");
        QCOMPARE(client.lastSent().seq, confirmed.seq);
        QCOMPARE(client.lastSent().buttons, uint32_t{kSquare});

        // (c) wedged route: the re-assert detects it and fails the pipe closed NOW.
        QCOMPARE(client.reassertLastState(),
                 InputRouteWriteResult::WrittenUnconfirmed);
        QVERIFY(!client.connected());
        QCOMPARE(client.ackFailures(), uint64_t{1});
        // The snapshot still holds the last CONFIRMED state; the next successful
        // reconnect reseeds from scratch (haveSent() is reset by ensureConnected).
        QCOMPARE(client.lastSent().buttons, uint32_t{kSquare});
        server.join();
        QCOMPARE(serverError.load(std::memory_order_relaxed), DWORD{ERROR_SUCCESS});
#else
        QSKIP("direct-input duplex transport is Windows-only");
#endif
    }

    void releaseMarkerCalibrationFlagIsExplicitAndAdditive()
    {
        const QJsonObject normal = makeReleaseMarkerCommand(17, 1234567.5);
        QCOMPARE(normal.value(QStringLiteral("cmd")).toString(),
                 QStringLiteral("release_marker"));
        QCOMPARE(normal.value(QStringLiteral("seq")).toInt(), 17);
        QCOMPARE(normal.value(QStringLiteral("wall_ms")).toDouble(), 1234567.5);
        QVERIFY(normal.contains(QStringLiteral("calibration")));
        QVERIFY(!normal.value(QStringLiteral("calibration")).toBool(true));

        const QJsonObject calibration = makeReleaseMarkerCommand(18, 1234568.5, true);
        QVERIFY(calibration.value(QStringLiteral("calibration")).toBool(false));
        QVERIFY(!calibration.contains(QStringLiteral("validation_target_pct")));

        const QJsonObject validation = makeReleaseMarkerCommand(
            19, 1234569.5, true, 88.0, 3.5, 701, 9007199254740993ULL);
        QCOMPARE(validation.value(QStringLiteral("validation_target_pct")).toDouble(), 88.0);
        QCOMPARE(validation.value(QStringLiteral("validation_tolerance_pct")).toDouble(), 3.5);
        QCOMPARE(validation.value(QStringLiteral("physical_epoch")).toString(),
                 QStringLiteral("701"));
        QCOMPARE(validation.value(QStringLiteral("shot_attempt")).toString(),
                 QStringLiteral("9007199254740993"));
    }
};

QTEST_APPLESS_MAIN(OrionInputProtocolTests)

#include "OrionInputProtocolTests.moc"
