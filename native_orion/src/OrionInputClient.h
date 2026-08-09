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
#include <cstdint>
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

    const bool buttonOrTriggerEdge = previous->buttons != current.buttons
        || previous->l2_state != current.l2_state
        || previous->r2_state != current.r2_state;
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
    [[nodiscard]] uint64_t writeCount() const { return writeCount_.load(std::memory_order_relaxed); }
    [[nodiscard]] uint64_t writeFailures() const { return writeFailures_.load(std::memory_order_relaxed); }
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
        const ControllerState& state, bool own, bool forceWrite = false);

private:
    bool ensureConnected();
    void closePipe();
#ifdef _WIN32
    InputRouteWriteResult waitForDeliveryAck(uint32_t sourceSeq, bool ownedPacket);
#endif

    QString pipeName_;
    // A write and both of its ACK reads are one transaction. Protect the shared
    // sequence, named-pipe handle, OVERLAPPED events, and last-state snapshot so
    // a second caller can never consume or cancel the first caller's ACK.
    mutable std::mutex ioMutex_;
    std::atomic<bool> enabled_{false};
    uint32_t seq_ = 0;
    bool haveLast_ = false;
    OrionInputPacket last_{};
    std::atomic<uint64_t> writeCount_{0};
    std::atomic<uint64_t> writeFailures_{0};
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
#ifdef _WIN32
    HANDLE pipe_ = INVALID_HANDLE_VALUE;
    HANDLE writeEvent_ = nullptr;   // overlapped-write completion event (P4: bounded, non-blocking write)
    HANDLE readEvent_ = nullptr;    // independent overlapped ACK completion event
    double lastConnectAttemptMs_ = -1.0;
#endif
};

} // namespace orion
