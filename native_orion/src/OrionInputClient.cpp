#include "OrionInputClient.h"

#include <algorithm>
#include <cstring>
#include <memory>
#include <thread>

namespace orion {

namespace {
constexpr uint32_t kMagic = 0x4E49524FU; // 'ORIN'

// ChiakiControllerButton bits (mirror lib/include/chiaki/controller.h).
enum PsButton : uint32_t {
    PS_CROSS      = 1u << 0,
    PS_MOON       = 1u << 1,  // Circle
    PS_BOX        = 1u << 2,  // Square (the jump-shot button)
    PS_PYRAMID    = 1u << 3,  // Triangle
    PS_DPAD_LEFT  = 1u << 4,
    PS_DPAD_RIGHT = 1u << 5,
    PS_DPAD_UP    = 1u << 6,
    PS_DPAD_DOWN  = 1u << 7,
    PS_L1         = 1u << 8,
    PS_R1         = 1u << 9,
    PS_L3         = 1u << 10,
    PS_R3         = 1u << 11,
    PS_OPTIONS    = 1u << 12,
    PS_SHARE      = 1u << 13,
    PS_TOUCHPAD   = 1u << 14,
    PS_PS         = 1u << 15,
};

// XINPUT_GAMEPAD_GUIDE is already provided by OrionTypes.h (orion namespace).

// Sticks: Orion ControllerState is ~[-127,127] with UP = NEGATIVE internally (VirtualController
// flips Y only for ViGEm/XInput which is up=positive). chiaki/SDL is also UP = NEGATIVE, so the
// hook copies sticks with the SAME sign, scaled x258 to int16 (matches VirtualController::axisToShort).
int16_t axis(int v)
{
    return static_cast<int16_t>(std::clamp(v * 258, -32768, 32767));
}

uint32_t mapButtons(uint16_t x)
{
    uint32_t b = 0;
    if(x & XINPUT_GAMEPAD_A) b |= PS_CROSS;
    if(x & XINPUT_GAMEPAD_B) b |= PS_MOON;
    if(x & XINPUT_GAMEPAD_X) b |= PS_BOX;
    if(x & XINPUT_GAMEPAD_Y) b |= PS_PYRAMID;
    if(x & XINPUT_GAMEPAD_DPAD_UP) b |= PS_DPAD_UP;
    if(x & XINPUT_GAMEPAD_DPAD_DOWN) b |= PS_DPAD_DOWN;
    if(x & XINPUT_GAMEPAD_DPAD_LEFT) b |= PS_DPAD_LEFT;
    if(x & XINPUT_GAMEPAD_DPAD_RIGHT) b |= PS_DPAD_RIGHT;
    if(x & XINPUT_GAMEPAD_START) b |= PS_OPTIONS;
    if(x & XINPUT_GAMEPAD_BACK) b |= PS_SHARE;
    if(x & XINPUT_GAMEPAD_LEFT_THUMB) b |= PS_L3;
    if(x & XINPUT_GAMEPAD_RIGHT_THUMB) b |= PS_R3;
    if(x & XINPUT_GAMEPAD_LEFT_SHOULDER) b |= PS_L1;
    if(x & XINPUT_GAMEPAD_RIGHT_SHOULDER) b |= PS_R1;
    if(x & XINPUT_GAMEPAD_GUIDE) b |= PS_PS;
    return b;
}

bool sameInput(const OrionInputPacket& a, const OrionInputPacket& b)
{
    return a.buttons == b.buttons && a.left_x == b.left_x && a.left_y == b.left_y
        && a.right_x == b.right_x && a.right_y == b.right_y && a.l2_state == b.l2_state
        && a.r2_state == b.r2_state && a.own == b.own;
}

#ifdef _WIN32
// Max time the pipe write may block (it runs on the TIME_CRITICAL fire thread) before we abandon the
// packet. A write that ever became pending is classified ambiguous, because completion can race its
// cancellation; the caller keeps ViGEm neutral while OrionStream tears the session down.
constexpr DWORD kWriteTimeoutMs = 3;
// ACK delivery has two independently bounded phases. A pipe packet can spend most of one
// scheduler quantum waiting for OrionStream's bridge thread before ENQUEUED is emitted. Starting
// one 14 ms timer at WriteFile incorrectly left almost no budget for the server's separately
// bounded (10 ms) local-UDP handoff, so the client could close while the valid final ACK was being
// written. The final window starts only after the matching ENQUEUED proof. Both remain short and an
// unconfirmed packet still fails closed; no input packet is ever retried here.
constexpr DWORD kEnqueuedAckTimeoutMs = 25;
// Must exceed the bridge's bounded local-delivery wait (40 ms since 2026-09-22, was 25) plus pipe
// wakeup. This is only a failure ceiling; healthy ACKs still return immediately.
constexpr DWORD kFinalDeliveryAckTimeoutMs = 65;

// Every overlapped operation owns its buffer, event and duplicate pipe handle.
// After a deadline the caller cancels and returns; a separate reaper retains
// the storage until the kernel signals completion. Reconnection is capped while
// too many stuck cancellations are outstanding, bounding retained resources.
constexpr int kMaxPendingIoReaps = 8;
std::atomic<int> pendingIoReaps{0};
#ifdef ORION_INPUT_TEST_HOOKS
std::atomic<bool> forceDeferredCancelReapForTesting{false};
std::atomic<DWORD> cancelReapDelayMsForTesting{0};
std::atomic<int> gAbandonPendingWritesForTesting{0};
std::atomic<int> gAbandonTimedOutWritesForTesting{0};
#endif

struct OwnedPipeIo final {
    HANDLE pipe = INVALID_HANDLE_VALUE;
    HANDLE event = nullptr;
    OVERLAPPED overlapped{};
    OrionInputPacket packet{};
    OrionInputAck ack{};
    ~OwnedPipeIo()
    {
        if (event) CloseHandle(event);
        if (pipe != INVALID_HANDLE_VALUE) CloseHandle(pipe);
    }
};

struct PipeIoOutcome final {
    bool success = false;
    bool issued = false;
    DWORD error = ERROR_SUCCESS;
    DWORD bytes = 0;
    OrionInputAck ack{};
};

void reapPendingPipeIo(std::unique_ptr<OwnedPipeIo> operation) noexcept
{
    pendingIoReaps.fetch_add(1, std::memory_order_acq_rel);
    OwnedPipeIo* const retained = operation.release();
    try {
        std::thread([retained]() {
            std::unique_ptr<OwnedPipeIo> op(retained);
#ifdef ORION_INPUT_TEST_HOOKS
            const DWORD delay = cancelReapDelayMsForTesting.load(std::memory_order_acquire);
            if (delay) Sleep(delay);
#endif
            if (WaitForSingleObject(op->event, INFINITE) != WAIT_OBJECT_0) {
                // An invalid wait is not proof that the kernel stopped using
                // the OVERLAPPED storage. Retain it and refuse reconnection
                // once the global outstanding cap is reached.
                (void)op.release();
                return;
            } // never the caller/fire thread
            DWORD ignored = 0;
            (void)GetOverlappedResult(op->pipe, &op->overlapped, &ignored, FALSE);
            op.reset();
            pendingIoReaps.fetch_sub(1, std::memory_order_acq_rel);
        }).detach();
    } catch (...) {
        // Thread creation failed. The kernel may still reference the storage:
        // intentionally retain this one allocation until process exit rather
        // than free it, and the outstanding cap refuses further transactions.
        (void)retained;
    }
}

PipeIoOutcome runOwnedPipeIo(HANDLE sourcePipe, bool readAck,
                             const OrionInputPacket* packet, DWORD timeoutMs)
{
    PipeIoOutcome outcome;
    if (sourcePipe == INVALID_HANDLE_VALUE
        || pendingIoReaps.load(std::memory_order_acquire) >= kMaxPendingIoReaps) {
        outcome.error = ERROR_NOT_READY;
        return outcome;
    }
    auto op = std::make_unique<OwnedPipeIo>();
    op->event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!op->event || !DuplicateHandle(GetCurrentProcess(), sourcePipe,
                                      GetCurrentProcess(), &op->pipe, 0, FALSE,
                                      DUPLICATE_SAME_ACCESS)) {
        outcome.error = GetLastError();
        return outcome;
    }
    op->overlapped.hEvent = op->event;
    if (packet) op->packet = *packet;
    const BOOL issuedOk = readAck
        ? ReadFile(op->pipe, &op->ack, sizeof(op->ack), nullptr, &op->overlapped)
        : WriteFile(op->pipe, &op->packet, sizeof(op->packet), nullptr, &op->overlapped);
    const DWORD issueError = issuedOk ? ERROR_SUCCESS : GetLastError();
#ifdef ORION_INPUT_TEST_HOOKS
    const bool abandonWrite = !readAck && packet
        && (packet->reserved & OrionInputAbandon) != 0;
    if (abandonWrite && !issuedOk && issueError == ERROR_IO_PENDING)
        gAbandonPendingWritesForTesting.fetch_add(1, std::memory_order_relaxed);
#endif
    outcome.issued = issuedOk || issueError == ERROR_IO_PENDING;
    if (!outcome.issued) {
        outcome.error = issueError;
        return outcome;
    }
    if (!issuedOk) {
        const DWORD waited = WaitForSingleObject(op->event, timeoutMs);
        if (waited != WAIT_OBJECT_0) {
#ifdef ORION_INPUT_TEST_HOOKS
            if (abandonWrite && waited == WAIT_TIMEOUT)
                gAbandonTimedOutWritesForTesting.fetch_add(1, std::memory_order_relaxed);
#endif
            const DWORD waitError = waited == WAIT_TIMEOUT ? ERROR_TIMEOUT : GetLastError();
            (void)CancelIoEx(op->pipe, &op->overlapped);
            bool deferCompletion = WaitForSingleObject(op->event, 0) != WAIT_OBJECT_0;
#ifdef ORION_INPUT_TEST_HOOKS
            deferCompletion = deferCompletion
                || forceDeferredCancelReapForTesting.load(std::memory_order_acquire);
#endif
            if (deferCompletion) {
                outcome.error = waitError;
                reapPendingPipeIo(std::move(op));
                return outcome;
            }
        }
    }
    if (GetOverlappedResult(op->pipe, &op->overlapped, &outcome.bytes, FALSE)) {
        outcome.success = true;
        if (readAck) outcome.ack = op->ack;
    } else {
        outcome.error = GetLastError();
    }
    return outcome;
}

CheckedElapsedMicros qpcElapsedUs(
    BOOL startOk, const LARGE_INTEGER& start,
    BOOL endOk, const LARGE_INTEGER& end)
{
    struct Frequency final {
        bool valid = false;
        std::int64_t ticksPerSecond = 0;
    };
    static const Frequency freq = [] {
        LARGE_INTEGER f{};
        const BOOL ok = QueryPerformanceFrequency(&f);
        return Frequency{ok != FALSE, static_cast<std::int64_t>(f.QuadPart)};
    }();
    return checkedElapsedMicros(
        startOk != FALSE && freq.valid,
        endOk != FALSE,
        static_cast<std::int64_t>(start.QuadPart),
        static_cast<std::int64_t>(end.QuadPart),
        freq.ticksPerSecond);
}

void updateMax(std::atomic<uint64_t>& target, uint64_t value)
{
    uint64_t cur = target.load(std::memory_order_relaxed);
    while(value > cur && !target.compare_exchange_weak(cur, value, std::memory_order_relaxed)) {}
}
#endif
} // namespace

#ifdef ORION_INPUT_TEST_HOOKS
void OrionInputClient::configureDeferredCancellationForTesting(
    bool forceDeferred, DWORD reapDelayMs)
{
    forceDeferredCancelReapForTesting.store(forceDeferred, std::memory_order_release);
    cancelReapDelayMsForTesting.store(reapDelayMs, std::memory_order_release);
}

int OrionInputClient::pendingCancellationReapsForTesting()
{
    return pendingIoReaps.load(std::memory_order_acquire);
}

void OrionInputClient::resetAbandonWriteProbeForTesting()
{
    gAbandonPendingWritesForTesting.store(0, std::memory_order_release);
    gAbandonTimedOutWritesForTesting.store(0, std::memory_order_release);
}

int OrionInputClient::abandonPendingWritesForTesting()
{
    return gAbandonPendingWritesForTesting.load(std::memory_order_acquire);
}

int OrionInputClient::abandonTimedOutWritesForTesting()
{
    return gAbandonTimedOutWritesForTesting.load(std::memory_order_acquire);
}

HANDLE OrionInputClient::duplicatePipeHandleForTesting() const
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    HANDLE duplicate = INVALID_HANDLE_VALUE;
    if (pipe_ != INVALID_HANDLE_VALUE)
        (void)DuplicateHandle(GetCurrentProcess(), pipe_, GetCurrentProcess(),
                              &duplicate, 0, FALSE, DUPLICATE_SAME_ACCESS);
    return duplicate;
}
#endif

OrionInputClient::OrionInputClient(QString pipeName)
    : pipeName_(std::move(pipeName))
{
}

OrionInputClient::~OrionInputClient()
{
    {
        std::lock_guard<std::mutex> lock(ioMutex_);
        closePipe();
    }
}

bool OrionInputClient::connected() const
{
    std::lock_guard<std::mutex> lock(ioMutex_);
#ifdef _WIN32
    return pipe_ != INVALID_HANDLE_VALUE;
#else
    return false;
#endif
}

void OrionInputClient::resetConnection()
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    closePipe();
    haveLast_ = false;
    last_ = {};
    lastTransactionTiming_ = {};
#ifdef _WIN32
    // A replacement OrionStream may create its pipe immediately. Do not let a
    // failed attempt from the retiring generation impose the 500 ms throttle.
    lastConnectAttemptMs_ = -1.0;
#endif
}

OrionInputPacket OrionInputClient::lastSent() const
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    return last_;
}

bool OrionInputClient::haveSent() const
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    return haveLast_;
}

OrionInputTransactionTiming OrionInputClient::lastTransactionTiming() const
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    return lastTransactionTiming_;
}

#ifdef _WIN32
void OrionInputClient::closePipe()
{
    if(pipe_ != INVALID_HANDLE_VALUE)
    {
        CloseHandle(pipe_);
        pipe_ = INVALID_HANDLE_VALUE;
    }
}

bool OrionInputClient::ensureConnected()
{
    if (pendingIoReaps.load(std::memory_order_acquire) >= kMaxPendingIoReaps)
        return false;
    if(pipe_ != INVALID_HANDLE_VALUE)
        return true;
    // Throttle reconnect attempts (chiaki's pipe server may not be up yet).
    const double now = static_cast<double>(GetTickCount64());
    if(lastConnectAttemptMs_ >= 0.0 && (now - lastConnectAttemptMs_) < 500.0)
        return false;
    lastConnectAttemptMs_ = now;
    HANDLE h = CreateFileA(pipeName_.toLocal8Bit().constData(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                           OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
    if(h == INVALID_HANDLE_VALUE)
        return false;
    DWORD mode = PIPE_READMODE_MESSAGE;
    if(!SetNamedPipeHandleState(h, &mode, nullptr, nullptr))
    {
        CloseHandle(h);
        return false;
    }
    pipe_ = h;
    haveLast_ = false; // force a fresh state write on (re)connect
    return true;
}

bool OrionInputClient::send(const ControllerState& state, bool own)
{
    const InputRouteWriteResult result = sendDetailed(state, own);
    return result == InputRouteWriteResult::Written
        || result == InputRouteWriteResult::LocalUdpAccepted
        || result == InputRouteWriteResult::OwnershipReleased;
}

InputRouteWriteResult OrionInputClient::waitForDeliveryAck(uint32_t sourceSeq, bool ownedPacket)
{
    lastTransactionTiming_.beginAck();
    lastAckWaitUs_.store(0, std::memory_order_relaxed);
    LARGE_INTEGER ackStart{};
    const BOOL ackStartOk = QueryPerformanceCounter(&ackStart);
    OrionInputAck lastObservedAck{};
    bool haveObservedAck = false;

    const auto recordDiagnostics = [&](DWORD winError, DWORD bytes,
                                       const OrionInputAck* ack) {
        LARGE_INTEGER ackEnd{};
        const BOOL ackEndOk = QueryPerformanceCounter(&ackEnd);
        lastAckWinError_.store(winError, std::memory_order_relaxed);
        lastAckExpectedSeq_.store(sourceSeq, std::memory_order_relaxed);
        lastAckReadBytes_.store(bytes, std::memory_order_relaxed);
        lastAckSourceSeq_.store(ack ? ack->sourceSeq : 0, std::memory_order_relaxed);
        lastAckStage_.store(ack ? ack->stage : 0, std::memory_order_relaxed);
        lastAckProtocolError_.store(ack ? ack->error : 0, std::memory_order_relaxed);
        const CheckedElapsedMicros elapsed = qpcElapsedUs(
            ackStartOk, ackStart, ackEndOk, ackEnd);
        lastTransactionTiming_.recordAck(elapsed);
        if(elapsed.valid)
            lastAckWaitUs_.store(elapsed.value, std::memory_order_relaxed);
        else
        {
            lastAckWaitUs_.store(0, std::memory_order_relaxed);
            clockSampleFailures_.fetch_add(1, std::memory_order_relaxed);
        }
    };
    const auto fail = [&](DWORD winError, DWORD bytes,
                          const OrionInputAck* ack) {
        recordDiagnostics(winError, bytes, ack);
        ackFailures_.fetch_add(1, std::memory_order_relaxed);
        return InputRouteWriteResult::WrittenUnconfirmed;
    };

    if(pipe_ == INVALID_HANDLE_VALUE)
        return fail(ERROR_INVALID_HANDLE, 0, nullptr);

    ULONGLONG deadline = GetTickCount64() + kEnqueuedAckTimeoutMs;
    bool enqueuedObserved = false;
    while(GetTickCount64() <= deadline)
    {
        const ULONGLONG now = GetTickCount64();
        const DWORD remaining = now >= deadline
            ? 0 : static_cast<DWORD>(deadline - now);
        const PipeIoOutcome io = runOwnedPipeIo(pipe_, true, nullptr, remaining);
        const OrionInputAck ack = io.ack;
        const DWORD read = io.bytes;
        const bool readOk = io.success && read == sizeof(ack);
        const DWORD readError = readOk ? ERROR_SUCCESS
            : (io.success ? ERROR_INVALID_DATA : io.error);

        if(!readOk)
            return fail(readError, read, haveObservedAck ? &lastObservedAck : nullptr);

        lastObservedAck = ack;
        haveObservedAck = true;
        if(ack.magic != kOrionInputAckMagic)
            return fail(ERROR_INVALID_DATA, read, &ack);
        if(ack.sourceSeq != sourceSeq)
            continue; // discard an older completion left by a prior canceled caller
        const auto stage = static_cast<OrionInputAckStage>(ack.stage);
        if(stage == OrionInputAckStage::Failed)
            return fail(ERROR_GEN_FAILURE, read, &ack);
        if(stage == OrionInputAckStage::Enqueued)
        {
            if(!enqueuedObserved)
            {
                enqueuedObserved = true;
                deadline = GetTickCount64() + kFinalDeliveryAckTimeoutMs;
            }
            continue;
        }
        if(orionInputAckAccepts(ack, sourceSeq, ownedPacket))
        {
            recordDiagnostics(ERROR_SUCCESS, read, &ack);
            return ownedPacket ? InputRouteWriteResult::LocalUdpAccepted
                               : InputRouteWriteResult::OwnershipReleased;
        }

        return fail(ERROR_INVALID_DATA, read, &ack);
    }
    return fail(ERROR_TIMEOUT, 0, haveObservedAck ? &lastObservedAck : nullptr);
}

InputRouteWriteResult OrionInputClient::sendDetailed(
    const ControllerState& state, bool own, bool forceWrite,
    OrionInputTransactionTiming* transactionTiming)
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    if(transactionTiming)
        *transactionTiming = {};
    if(!enabled_.load(std::memory_order_acquire))
        return InputRouteWriteResult::Failed;
    if(!ensureConnected())
        return InputRouteWriteResult::Failed;

    const bool routeWasOwned = haveLast_ && last_.own != 0;

    OrionInputPacket p{};
    p.magic = kMagic;
    p.own = own ? 1 : 0;
    if(own)
    {
        p.buttons = mapButtons(state.buttons);
        // [2026-09-11] The touchpad click lives outside the XInput word (ControllerState::touchpad,
        // decoded from DualSense byte 10 bit 1) and was never put on the wire, so the console never
        // saw it. Bit 14 is CHIAKI_CONTROLLER_BUTTON_TOUCHPAD; the fork copies the mask verbatim.
        if(state.touchpad) p.buttons |= PS_TOUCHPAD;
        p.left_x = axis(state.leftStickX);
        p.left_y = axis(state.leftStickY);
        p.right_x = axis(state.rightStickX);
        p.right_y = axis(state.rightStickY);
        p.l2_state = state.l2;
        p.r2_state = state.r2;
    }

    p.reserved = classifyOrionInputPacketFlags(haveLast_ ? &last_ : nullptr, p);
    if(forceWrite)
        p.reserved |= OrionInputMustDeliver; // recovery proof requires downstream local acceptance

    // [2026-09-22 RED TEAM CL-003] A trigger release that shares a 4 ms poll with a NEW button press
    // is one packet on the wire, and the fork's release redundancy refuses to copy a packet that
    // contains a press (a duplicated press would be a phantom input). That left L2/R2 releases as
    // the one input class with no repair: one lost datagram and sprint stays down (the owner's
    // "R2 clamps down"). Send the trigger release FIRST as its own transaction - buttons exactly as
    // the console last saw them - so it qualifies as a pure release and gets the twin + echo; the
    // press follows as the next transaction. Both are MustDeliver, so ordering is proven, and the
    // console sees exactly the same two edges it would have merged.
    // [CL-003 Codex final gate 2026-09-22] `pre` is built from the LAST CONFIRMED packet plus only
    // terminal edges (trigger zero-crossings and digital releases), never from `p`: copying `p`
    // carried any simultaneous opposite-trigger increase into `pre`, and the fork refuses to copy
    // a release that contains a trigger increase (orioninput.c chiaki_orion_input_is_pure_button_
    // release), so R2-up + L2-rise still went out with no redundancy. The split runs whenever `p`
    // itself would NOT qualify as a copyable release (a new press OR a trigger increase); a trigger
    // release accompanied only by stick motion already qualifies on its own and is not split.
    if(own && haveLast_ && !forceWrite && last_.own == p.own)
    {
        const bool l2Released = last_.l2_state != 0 && p.l2_state == 0;
        const bool r2Released = last_.r2_state != 0 && p.r2_state == 0;
        const uint32_t newlyPressed = p.buttons & ~last_.buttons;
        const bool triggerIncreased = p.l2_state > last_.l2_state || p.r2_state > last_.r2_state;
        if((l2Released || r2Released) && (newlyPressed != 0 || triggerIncreased))
        {
            OrionInputPacket pre = last_;
            pre.buttons = last_.buttons & p.buttons;   // releases only; never a new press
            if(l2Released)
                pre.l2_state = 0;
            if(r2Released)
                pre.r2_state = 0;
            pre.reserved = classifyOrionInputPacketFlags(&last_, pre) | OrionInputMustDeliver;
            const InputRouteWriteResult preResult = transmitLocked(pre, own, routeWasOwned);
            if(preResult == InputRouteWriteResult::Failed
                || preResult == InputRouteWriteResult::WrittenUnconfirmed)
                return preResult;
            last_ = pre;
            haveLast_ = true;
            splitTriggerReleases_.fetch_add(1, std::memory_order_relaxed);
            p.reserved = classifyOrionInputPacketFlags(&last_, p);
        }
    }

    // De-dupe: only write when the mapped packet actually changes (a button press / stick move / an
    // ownership flip). Orion drives CONTINUOUS ownership (own=1 every tick: the patched chiaki doesn't
    // pump SDL, so its own=0/ViGEm-mirror path is dead -- see OrionAppController), and chiaki holds the
    // last injected state between writes, so on-change writes suffice and an idle hold sends nothing.
    //
    // INVARIANT (a physical press must always be able to reach the console): this suppression is
    // sound ONLY while `last_` provably matches what OrionStream holds. That is guaranteed because
    //  * `last_` advances solely below, after a successful write AND (for every button/trigger/own/
    //    shot-band edge, which classifyOrionInputPacketFlags marks MustDeliver) after the exact
    //    seq-matched local-delivery ACK;
    //  * every write failure or ambiguity closes the pipe without touching `last_`, and
    //    ensureConnected()/resetConnection() clear `haveLast_` so the first packet on any new or
    //    reconnected route is a forced full-state MustDeliver seed (previous==nullptr above);
    //  * reassertLastState() re-proves the established route at idle so a divergence that escapes
    //    the rules above (or a silently wedged reader) is detected/healed within its call cadence
    //    instead of at the player's next press.
    // No optimisation here may ever suppress a state change the console has not provably received.
    if(!forceWrite && haveLast_ && sameInput(last_, p))
    {
        // [CL2-P4-001 2026-09-22] Dead-man keepalive. An unchanged owned state sends nothing, so
        // the fork cannot tell "the player is holding still" from "the launcher's input loop has
        // stalled" - and a stall used to leave the console holding the last input indefinitely.
        // While owned, re-send the exact confirmed state UNFLAGGED at least every
        // kOwnedKeepaliveMs; the fork neutralises after 400 ms of silence. Unflagged = latest-wins,
        // no ACK wait, console-visible state unchanged. This runs on the input tick itself, so it
        // stops exactly when that tick stops. last_ is untouched (it already holds this state).
        if(own && last_.own != 0
            && std::chrono::steady_clock::now() - lastWireWriteAt_
                   >= std::chrono::milliseconds(kOwnedKeepaliveMs))
        {
            OrionInputPacket keepalive = last_;
            keepalive.reserved = 0;
            const InputRouteWriteResult kaResult = transmitLocked(keepalive, own, routeWasOwned);
            if(kaResult == InputRouteWriteResult::Failed
                || kaResult == InputRouteWriteResult::WrittenUnconfirmed)
                return kaResult;
            ownedKeepalives_.fetch_add(1, std::memory_order_relaxed);
        }
        return InputRouteWriteResult::Unchanged;
    }

    const InputRouteWriteResult result = transmitLocked(p, own, routeWasOwned);
    if(transactionTiming)
        *transactionTiming = lastTransactionTiming_;
    if(result == InputRouteWriteResult::Failed
        || result == InputRouteWriteResult::WrittenUnconfirmed)
        return result;
    last_ = p;
    haveLast_ = true;
    return result;
}

OrionInputClient::SquareWatchdogReleaseResult OrionInputClient::releaseSquareForWatchdog()
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    SquareWatchdogReleaseResult outcome;
    constexpr uint32_t kSquareBit = 1u << 2;
    if (!enabled_.load(std::memory_order_acquire)
        || pipe_ == INVALID_HANDLE_VALUE
        || !haveLast_ || last_.own == 0 || (last_.buttons & kSquareBit) == 0)
        return outcome;
    for (int copy = 0; copy < 2; ++copy) {
        OrionInputPacket packet = last_;
        packet.buttons &= ~kSquareBit;
        packet.reserved = OrionInputMustDeliver | OrionInputShotRelease;
        ++outcome.attempted;
        const auto result = transmitLocked(packet, true, true);
        if (result != InputRouteWriteResult::LocalUdpAccepted)
            break; // Ambiguous generation is closed; never reseed it here.
        last_ = packet;
        ++outcome.accepted;
    }
    return outcome;
}

InputRouteWriteResult OrionInputClient::reassertLastState()
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    if(!enabled_.load(std::memory_order_acquire))
        return InputRouteWriteResult::Failed;
    // Liveness proof of an ESTABLISHED, previously-confirmed route only. Never connect or seed
    // here: a replacement OrionStream must receive its first owned packet through the ordinary
    // send path once its session is provably Running (see ensureConnected/resetConnection).
    if(pipe_ == INVALID_HANDLE_VALUE)
        return InputRouteWriteResult::Failed;
    if(!haveLast_ || last_.own == 0)
        return InputRouteWriteResult::Unchanged;

    // Re-send the exact last confirmed state as a fresh MustDeliver transaction. The console-visible
    // state is unchanged (identical packet; the bridge/feedback sender treats an equal state as a
    // no-op re-assert), so this is idempotent — its purpose is the ACK round-trip: success bounds
    // any client<->OrionStream divergence to one call interval, and failure closes the pipe HERE,
    // at an idle moment, so the wedge is repaired by the ordinary recovery machinery BEFORE the
    // player's next press instead of being discovered by it. `last_` is deliberately untouched:
    // it already holds this state, confirmed.
    OrionInputPacket p = last_;
    p.reserved = OrionInputMustDeliver;
    return transmitLocked(p, /*own=*/true, /*routeWasOwned=*/true);
}

// Core single-packet transaction: sequence stamp, bounded overlapped write, and (for MustDeliver /
// ownership-release packets) the exact ACK wait. Fails closed exactly like the original inline
// code: any failure or ambiguity closes the pipe and reports it; the CALLER decides whether the
// de-dup snapshot advances. Must be called with ioMutex_ held.
InputRouteWriteResult OrionInputClient::transmitLocked(
    OrionInputPacket& p, bool own, bool routeWasOwned)
{
    p.seq = ++seq_;
    lastWireWriteAt_ = std::chrono::steady_clock::now();   // [CL2-P4-001] keepalive clock
    lastTransactionTiming_.begin(p.seq);
    lastWriteUs_.store(0, std::memory_order_relaxed);
    DWORD written = 0;
    LARGE_INTEGER start{}, end{};
    const BOOL startOk = QueryPerformanceCounter(&start);

    // A timed-out operation retains its own OVERLAPPED/buffer/handle in an
    // asynchronous reaper. The fire caller never waits for cancellation.
    const PipeIoOutcome io = runOwnedPipeIo(pipe_, false, &p, kWriteTimeoutMs);
    written = io.bytes;
    const bool writeOk = io.success && written == sizeof(p);
    const bool writeMayHaveBeenAccepted = io.issued;

    const BOOL endOk = QueryPerformanceCounter(&end);
    const CheckedElapsedMicros elapsed = qpcElapsedUs(startOk, start, endOk, end);
    lastTransactionTiming_.recordWrite(elapsed);
    if(elapsed.valid)
    {
        lastWriteUs_.store(elapsed.value, std::memory_order_relaxed);
        updateMax(maxWriteUs_, elapsed.value);
    }
    else
    {
        lastWriteUs_.store(0, std::memory_order_relaxed);
        clockSampleFailures_.fetch_add(1, std::memory_order_relaxed);
    }
    if(!writeOk)
    {
        writeFailures_.fetch_add(1, std::memory_order_relaxed);
        closePipe();
        return inputRouteWriteFailureResult(routeWasOwned, writeMayHaveBeenAccepted);
    }
    writeCount_.fetch_add(1, std::memory_order_relaxed);

    InputRouteWriteResult result = InputRouteWriteResult::Written;
    if((p.reserved & OrionInputMustDeliver) != 0 || !own)
    {
        result = waitForDeliveryAck(p.seq, own);
        if(result == InputRouteWriteResult::WrittenUnconfirmed)
        {
            // Do not race an immediate ViGEm fallback against a possibly queued direct packet.
            // [CL2-P2-003] Announce the abandonment first, so OrionStream can tell "the launcher
            // gave up on seq N" (soft, bounded, session kept) from "the launcher died" (fatal).
            sendAbandonLocked(p.seq, own);
            closePipe();
        }
    }
    return result;
}

void OrionInputClient::sendAbandonLocked(uint32_t abandonedSeq, bool own)
{
    if(pipe_ == INVALID_HANDLE_VALUE)
        return;
    OrionInputPacket abandon{};
    if(haveLast_)
        abandon = last_;   // last CONFIRMED state: harmless to a fork that ignores the flag
    abandon.magic = kMagic;
    abandon.own = own ? 1 : 0;
    abandon.seq = abandonedSeq;
    abandon.reserved = OrionInputAbandon;
    const PipeIoOutcome io = runOwnedPipeIo(pipe_, false, &abandon, kWriteTimeoutMs);
    // A timed-out notice is terminally unconfirmed; the background reaper never
    // increments this counter even if completion wins later.
    if(io.success && io.bytes == sizeof(abandon))
        abandonsSent_.fetch_add(1, std::memory_order_relaxed);
}
#else
void OrionInputClient::closePipe() {}
void OrionInputClient::sendAbandonLocked(uint32_t, bool) {}
bool OrionInputClient::ensureConnected() { return false; }
bool OrionInputClient::send(const ControllerState&, bool) { return false; }
OrionInputClient::SquareWatchdogReleaseResult OrionInputClient::releaseSquareForWatchdog() { return {}; }
InputRouteWriteResult OrionInputClient::sendDetailed(
    const ControllerState&, bool, bool, OrionInputTransactionTiming* transactionTiming)
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    if(transactionTiming)
        *transactionTiming = {};
    return InputRouteWriteResult::Failed;
}
InputRouteWriteResult OrionInputClient::reassertLastState()
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    return InputRouteWriteResult::Failed;
}
#endif

} // namespace orion
