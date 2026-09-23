#pragma once

// Orion side of the pre-encryption INPUT HOOK. Writes the engine's computed ControllerState to the
// patched chiaki-ng over a Windows named pipe (chiaki injects it straight into the session,
// bypassing the ViGEm/SDL read path -> low-jitter release timing). Critical edges receive an exact,
// sequence-tagged local-delivery ACK. ViGEm is used only when this route is known not to own input;
// an ambiguous write fails closed instead of racing both routes. Default OFF.

#include "OrionTypes.h"
#include "PreciseFirePolicy.h"

#include <QString>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <limits>
#include <mutex>

#ifdef _WIN32
#include <windows.h>
#endif

namespace orion {

// Wire format — MUST stay byte-identical to chiaki-ng gui/include/orioninputbridge.h OrionInputPacket.
#pragma pack(push, 1)
struct OrionInputPacket {
    uint32_t magic;       // ORION_INPUT_MAGIC ('ORIN')
    uint32_t seq;         // monotonic
    uint32_t buttons;     // ChiakiControllerButton bitmask
    int16_t left_x, left_y, right_x, right_y;
    uint8_t l2_state, r2_state;
    uint8_t own;          // 1 = Orion drives input; 0 = release ownership (chiaki falls back to ViGEm/SDL)
    uint8_t reserved;     // OrionInputPacketFlag bitmask; wire size remains 24 bytes
};
#pragma pack(pop)
static_assert(sizeof(OrionInputPacket) == 24, "OrionInputPacket wire size must stay 24 bytes");

// Delivery metadata carried in the packet's existing reserved byte. These
// flags are local Orion <-> OrionStream protocol only; they do not alter the
// encrypted Remote Play wire format.
enum OrionInputPacketFlag : uint8_t {
    OrionInputMustDeliver = 1u << 0,
    OrionInputShotRelease = 1u << 1,
    OrionInputRedundantFlick = 1u << 2,
    // [CL2-P2-003 2026-09-22] "The launcher gave up waiting for the ACK of packet `seq`, and is
    // about to close the pipe." Sent once, unacknowledged, immediately before a client-side ACK
    // timeout closes the pipe, so OrionStream can take its bounded SOFT path (drop the unsent
    // queue, keep the ownership latch, re-listen) instead of treating the close as a dead launcher
    // and stopping the console session. Carries the last CONFIRMED state, so a fork that predates
    // the flag sees an equal-state re-assert and nothing else.
    OrionInputAbandon = 1u << 3,
};

constexpr uint32_t kOrionInputAckMagic = 0x4B43414FU; // 'OACK'
enum class OrionInputAckStage : uint8_t {
    Enqueued = 1,
    LocalUdpAccepted = 2,
    OwnershipReleased = 3,
    Failed = 0x80,
};

#pragma pack(push, 1)
struct OrionInputAck {
    uint32_t magic;
    uint32_t sourceSeq;
    uint8_t stage;
    uint8_t error;
    uint16_t reserved;
};
#pragma pack(pop)
static_assert(sizeof(OrionInputAck) == 12, "OrionInputAck wire size must stay 12 bytes");

[[nodiscard]] inline bool orionInputAckAccepts(
    const OrionInputAck& ack, uint32_t sourceSeq, bool ownedPacket) noexcept
{
    if (ack.magic != kOrionInputAckMagic || ack.sourceSeq != sourceSeq) {
        return false;
    }
    const auto stage = static_cast<OrionInputAckStage>(ack.stage);
    return ownedPacket ? stage == OrionInputAckStage::LocalUdpAccepted
                       : stage == OrionInputAckStage::OwnershipReleased;
}

// Checked conversion shared by the real QPC diagnostics and unit tests.  Keep
// the counter values signed until ordering has been proven: casting a negative
// QPC delta directly to uint64_t produced 18446744073709...us samples and
// permanently poisoned the max telemetry.  Invalid samples are explicitly
// distinguishable from a legitimate sub-microsecond measurement of zero.
struct CheckedElapsedMicros final {
    bool valid = false;
    std::uint64_t value = 0;
};

[[nodiscard]] constexpr CheckedElapsedMicros checkedElapsedMicros(
    bool startClockOk,
    bool endClockOk,
    std::int64_t startTicks,
    std::int64_t endTicks,
    std::int64_t ticksPerSecond) noexcept
{
    if (!startClockOk || !endClockOk || ticksPerSecond <= 0 || endTicks < startTicks) {
        return {};
    }

    // Use floating-point only for the scale conversion.  Subtraction is done
    // unsigned after signed ordering validation, avoiding signed overflow even
    // for synthetic boundary-value tests.
    const std::uint64_t deltaTicks = static_cast<std::uint64_t>(endTicks)
        - static_cast<std::uint64_t>(startTicks);
    const std::uint64_t frequency = static_cast<std::uint64_t>(ticksPerSecond);
    const std::uint64_t wholeSeconds = deltaTicks / frequency;
    constexpr std::uint64_t kMicrosPerSecond = 1'000'000;
    if (wholeSeconds > std::numeric_limits<std::uint64_t>::max() / kMicrosPerSecond) {
        return {};
    }
    const std::uint64_t remainderTicks = deltaTicks % frequency;
    // The fractional result is strictly below one second, so this conversion
    // cannot overflow even on MSVC where long double has double precision.
    const auto fractionalMicros = static_cast<std::uint64_t>(
        static_cast<long double>(remainderTicks) * static_cast<long double>(kMicrosPerSecond)
        / static_cast<long double>(frequency));
    const std::uint64_t wholeMicros = wholeSeconds * kMicrosPerSecond;
    if (fractionalMicros > std::numeric_limits<std::uint64_t>::max() - wholeMicros) {
        return {};
    }
    return {true, wholeMicros + fractionalMicros};
}

// One immutable timing record per transmitted packet.  The source sequence is
// the join key: a caller never has to combine the current packet identity with
// a process-global "last latency" value that may belong to an earlier packet.
// Attempted and valid are separate so a real 0 us sample is distinguishable
// from both "not measured" and "clock failed/regressed".
struct OrionInputTransactionTiming final {
    std::uint32_t sourceSeq = 0;
    bool writeAttempted = false;
    bool writeSampleValid = false;
    std::uint64_t writeUs = 0;
    bool ackAttempted = false;
    bool ackSampleValid = false;
    std::uint64_t ackWaitUs = 0;

    void begin(std::uint32_t seq) noexcept
    {
        *this = {};
        sourceSeq = seq;
    }

    void recordWrite(CheckedElapsedMicros sample) noexcept
    {
        writeAttempted = true;
        writeSampleValid = sample.valid;
        writeUs = sample.valid ? sample.value : 0;
    }

    void beginAck() noexcept
    {
        ackAttempted = true;
        ackSampleValid = false;
        ackWaitUs = 0;
    }

    void recordAck(CheckedElapsedMicros sample) noexcept
    {
        ackAttempted = true;
        ackSampleValid = sample.valid;
        ackWaitUs = sample.valid ? sample.value : 0;
    }

    [[nodiscard]] bool matches(std::uint32_t seq) const noexcept
    {
        return seq != 0 && sourceSeq == seq;
    }
};

// Generated stick shots use full-scale, vertical right-stick states. Preserve
// transitions into, out of, or across those bands as ordered edges while
// ordinary analog movement remains latest-wins.
[[nodiscard]] inline int orionVerticalShotBand(const OrionInputPacket& packet) noexcept
{
    constexpr int kShotAxisThreshold = 24000;
    const int x = static_cast<int>(packet.right_x);
    const int y = static_cast<int>(packet.right_y);
    const int absX = x < 0 ? -x : x;
    const int absY = y < 0 ? -y : y;
    if (absY < kShotAxisThreshold || absY < (absX * 2)) {
        return 0;
    }
    return y < 0 ? -1 : 1;
}

[[nodiscard]] inline uint8_t classifyOrionInputPacketFlags(
    const OrionInputPacket* previous,
    const OrionInputPacket& current) noexcept
{
    if (!previous) {
        // A reconnect must promptly seed OrionStream with the complete current
        // state instead of waiting for the ordinary movement poll.
        return OrionInputMustDeliver;
    }

    uint8_t flags = 0;
    if (previous->own != current.own) {
        flags |= OrionInputMustDeliver;
    }

    // [2026-09-22 RED TEAM CL-010] Only a trigger ZERO-CROSSING is an edge the console must not
    // miss (pressed <-> released). Intermediate analog travel is latest-wins like the sticks: the
    // fork still forwards every step, but without a synchronous local-delivery ACK per step, which
    // under an L2/R2 ramp was a burst of blocking transactions feeding the 25 ms budget.
    const bool l2Crossing = (previous->l2_state == 0) != (current.l2_state == 0);
    const bool r2Crossing = (previous->r2_state == 0) != (current.r2_state == 0);
    const bool buttonOrTriggerEdge = previous->buttons != current.buttons
        || l2Crossing || r2Crossing;
    if (buttonOrTriggerEdge) {
        flags |= OrionInputMustDeliver;
        // Chiaki's Square/BOX bit is bit 2. Other button/trigger releases still
        // need ordered delivery, but they are not shot-release commands and must
        // not enter the transport's bounded release-reliability path.
        constexpr uint32_t kPsBox = 1u << 2;
        const bool squareReleased = (previous->buttons & kPsBox) != 0
            && (current.buttons & kPsBox) == 0;
        if (squareReleased) {
            flags |= OrionInputShotRelease;
        }
    }

    const int previousBand = orionVerticalShotBand(*previous);
    const int currentBand = orionVerticalShotBand(current);
    if (previousBand != currentBand && (previousBand != 0 || currentBand != 0)) {
        flags |= OrionInputMustDeliver;
        // Neutral -> full deflection is the gather. Leaving that gather band,
        // reversing it for Tempo, or neutralising a flick is a release edge.
        if (previousBand != 0) {
            flags |= OrionInputShotRelease;
            if (currentBand != 0) {
                // Only a non-neutral reversal is the Tempo flick that benefits from one
                // fresh-sequence reliability copy. Terminal neutral remains single-send.
                flags |= OrionInputRedundantFlick;
            }
        }
    }
    return flags;
}

class OrionInputClient {
public:
    struct SquareWatchdogReleaseResult {
        int attempted = 0;
        int accepted = 0;
    };
    explicit OrionInputClient(QString pipeName = QStringLiteral("\\\\.\\pipe\\orion_input"));
    ~OrionInputClient();

    void setEnabled(bool e) { enabled_.store(e, std::memory_order_release); }
    [[nodiscard]] bool enabled() const { return enabled_.load(std::memory_order_acquire); }
    [[nodiscard]] bool connected() const;
    // End the current server-process generation. A named-pipe HANDLE can remain
    // locally non-null after OrionStream exits; carrying it (and the de-dup
    // snapshot) into a replacement process can suppress the first seed packet
    // and falsely keep the desktop ViGEm route neutral. Call only at an explicit
    // Remote Play teardown/restart boundary.
    void resetConnection();
    // DIAGNOSTIC: the last packet actually written to the pipe (drift debugging).
    [[nodiscard]] OrionInputPacket lastSent() const;
    [[nodiscard]] bool haveSent() const;
    // Coherent copy of the most recent actual pipe transaction. Unlike the
    // compatibility scalar getters below, validity and source sequence travel
    // with the durations under ioMutex_.
    [[nodiscard]] OrionInputTransactionTiming lastTransactionTiming() const;
    [[nodiscard]] uint64_t writeCount() const { return writeCount_.load(std::memory_order_relaxed); }
    [[nodiscard]] uint64_t writeFailures() const { return writeFailures_.load(std::memory_order_relaxed); }
    uint64_t splitTriggerReleases() const noexcept { return splitTriggerReleases_.load(std::memory_order_relaxed); }
    // [CL2-P4-001 2026-09-22] owned keepalives sent while the state was unchanged (diagnostic).
    uint64_t ownedKeepalives() const noexcept { return ownedKeepalives_.load(std::memory_order_relaxed); }
    // [CL2-P2-003] abandon notices sent before a client-side ACK-timeout close (diagnostic).
    uint64_t abandonsSent() const noexcept { return abandonsSent_.load(std::memory_order_relaxed); }
    // The fork neutralises an owned route after this much pipe silence, so the launcher must write
    // at least this often while it owns input (dead-man switch, fork half in orioninputbridge.cpp).
    static constexpr int kOwnedKeepaliveMs = 100;
#ifdef ORION_INPUT_TEST_HOOKS
    static void configureDeferredCancellationForTesting(bool forceDeferred, DWORD reapDelayMs);
    [[nodiscard]] static int pendingCancellationReapsForTesting();
    // Real named-pipe saturation fixture only: duplicate the established client
    // endpoint without transferring ownership from this instance.
    [[nodiscard]] HANDLE duplicatePipeHandleForTesting() const;
    static void resetAbandonWriteProbeForTesting();
    [[nodiscard]] static int abandonPendingWritesForTesting();
    [[nodiscard]] static int abandonTimedOutWritesForTesting();
#endif
    [[nodiscard]] uint64_t lastWriteUs() const { return lastWriteUs_.load(std::memory_order_relaxed); }
    [[nodiscard]] uint64_t maxWriteUs() const { return maxWriteUs_.load(std::memory_order_relaxed); }
    [[nodiscard]] uint64_t ackFailures() const { return ackFailures_.load(std::memory_order_relaxed); }
    [[nodiscard]] uint32_t lastAckWinError() const {
        return lastAckWinError_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] uint32_t lastAckSourceSeq() const {
        return lastAckSourceSeq_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] uint32_t lastAckExpectedSeq() const {
        return lastAckExpectedSeq_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] uint32_t lastAckReadBytes() const {
        return lastAckReadBytes_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] uint32_t lastAckStage() const {
        return lastAckStage_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] uint32_t lastAckProtocolError() const {
        return lastAckProtocolError_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] uint64_t lastAckWaitUs() const {
        return lastAckWaitUs_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] uint64_t clockSampleFailures() const {
        return clockSampleFailures_.load(std::memory_order_relaxed);
    }

    // Map `state` (XInput-style) -> chiaki PS ControllerState and write it to the pipe. own=true means
    // Orion is driving (chiaki uses this state); own=false relinquishes to chiaki's normal input.
    // De-duped: writes only when the mapped packet changes, so a held position sends once and chiaki
    // holds the last injected state between writes (its feedback sender re-asserts it on its own
    // keep-alive). Returns true if a write occurred.
    bool send(const ControllerState& state, bool own);
    // Same operation with enough outcome detail for the precise-fire worker to
    // distinguish an already-latched state from a broken route. send() remains
    // the compatibility wrapper whose return value means bytes were written.
    InputRouteWriteResult sendDetailed(
        const ControllerState& state, bool own, bool forceWrite = false,
        OrionInputTransactionTiming* transactionTiming = nullptr);
    // Idle-route liveness proof + divergence self-heal. Re-sends the exact last
    // CONFIRMED owned state as a fresh MustDeliver transaction over the already
    // established pipe (never connects/seeds). Idempotent for the console; the
    // value is the ACK round-trip: success bounds any client<->OrionStream
    // de-dup divergence to one call interval, failure closes the pipe at an
    // idle moment so recovery repairs the route BEFORE the player's next press.
    // Returns Unchanged when there is no confirmed owned state to re-assert,
    // Failed when disabled/disconnected. Call cadence is the caller's budget
    // decision (production: 1 Hz from the hook heartbeat, engine Idle only).
    InputRouteWriteResult reassertLastState();
    // Two fresh-sequence Square-up transactions on the ESTABLISHED owned pipe
    // only. Preserve other controls, bypass de-dup, require exact ACKs, and
    // stop on ambiguity without reconnecting or switching routes.
    SquareWatchdogReleaseResult releaseSquareForWatchdog();

private:
    bool ensureConnected();
    void closePipe();
    // [CL2-P2-003] One unacknowledged OrionInputAbandon packet, then the caller closes. ioMutex_ held.
    void sendAbandonLocked(uint32_t abandonedSeq, bool own);
#ifdef _WIN32
    InputRouteWriteResult waitForDeliveryAck(uint32_t sourceSeq, bool ownedPacket);
    // Sequence-stamp + bounded overlapped write + exact ACK wait for one packet.
    // Fails closed (closes the pipe) on any failure/ambiguity. Never advances
    // the de-dup snapshot — that is the caller's decision. ioMutex_ must be held.
    InputRouteWriteResult transmitLocked(OrionInputPacket& p, bool own, bool routeWasOwned);
#endif

    QString pipeName_;
    // A write and both of its ACK reads are one transaction. Protect the shared
    // sequence, named-pipe handle, and last-state snapshot so
    // a second caller can never consume or cancel the first caller's ACK.
    mutable std::mutex ioMutex_;
    std::atomic<bool> enabled_{false};
    uint32_t seq_ = 0;
    bool haveLast_ = false;
    OrionInputPacket last_{};
    OrionInputTransactionTiming lastTransactionTiming_{};
    std::atomic<uint64_t> writeCount_{0};
    std::atomic<uint64_t> writeFailures_{0};
    // [2026-09-22 CL-003] trigger releases sent ahead of a simultaneous press (diagnostic).
    std::atomic<uint64_t> splitTriggerReleases_{0};
    std::atomic<uint64_t> ownedKeepalives_{0};
    std::atomic<uint64_t> abandonsSent_{0};
    std::chrono::steady_clock::time_point lastWireWriteAt_{};   // guarded by ioMutex_
    std::atomic<uint64_t> lastWriteUs_{0};
    std::atomic<uint64_t> maxWriteUs_{0};
    std::atomic<uint64_t> ackFailures_{0};
    std::atomic<uint32_t> lastAckWinError_{0};
    std::atomic<uint32_t> lastAckSourceSeq_{0};
    std::atomic<uint32_t> lastAckExpectedSeq_{0};
    std::atomic<uint32_t> lastAckReadBytes_{0};
    std::atomic<uint32_t> lastAckStage_{0};
    std::atomic<uint32_t> lastAckProtocolError_{0};
    std::atomic<uint64_t> lastAckWaitUs_{0};
    // QPC failure/regression samples are excluded from latency values and
    // counted here so telemetry can distinguish "zero us" from "no sample".
    std::atomic<uint64_t> clockSampleFailures_{0};
#ifdef _WIN32
    HANDLE pipe_ = INVALID_HANDLE_VALUE;
    double lastConnectAttemptMs_ = -1.0;
#endif
};

} // namespace orion
