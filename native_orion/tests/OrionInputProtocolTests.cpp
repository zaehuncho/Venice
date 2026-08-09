#include "OrionInputClient.h"
#include "LatencyCalibrationSetupPolicy.h"
#include "PressedOverlayPolicy.h"
#include "LiveMeterHudPolicy.h"
#include "ReleaseMarkerDeliveryGate.h"
#include "ReleaseMarkerProtocol.h"
#include "ShotIntentPolicy.h"
#include "SidecarLogRelayPolicy.h"
#include "SidecarWatchdog.h"

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
        const InputRouteWriteResult release = client.sendDetailed(
            neutral, false, /*forceWrite=*/true);
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
        const InputRouteWriteResult result = client.sendDetailed(
            state, true, /*forceWrite=*/true);
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

    void deliveryStageFieldsStayParserStable()
    {
        QVERIFY(!PreciseFirePolicy::requiresExactDeliveryAck(false, false, false));
        QVERIFY(PreciseFirePolicy::requiresExactDeliveryAck(true, false, false));
        QVERIFY(!PreciseFirePolicy::requiresExactDeliveryAck(true, true, false));
        QVERIFY(PreciseFirePolicy::requiresExactDeliveryAck(false, false, true));
        QVERIFY(PreciseFirePolicy::requiresExactDeliveryAck(true, true, true));

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
