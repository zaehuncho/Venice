#include "OrionInputClient.h"

#include <algorithm>
#include <cstring>

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
// Must exceed the bridge's bounded 25 ms local-delivery wait plus pipe wakeup.
// This is only a failure ceiling; healthy ACKs still return immediately.
constexpr DWORD kFinalDeliveryAckTimeoutMs = 50;

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

OrionInputClient::OrionInputClient(QString pipeName)
    : pipeName_(std::move(pipeName))
{
#ifdef _WIN32
    // Manual-reset completion event, created once and reused for every overlapped write.
    writeEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    readEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
#endif
}

OrionInputClient::~OrionInputClient()
{
    {
        std::lock_guard<std::mutex> lock(ioMutex_);
        closePipe();
    }
#ifdef _WIN32
    if(writeEvent_)
    {
        CloseHandle(writeEvent_);
        writeEvent_ = nullptr;
    }
    if(readEvent_)
    {
        CloseHandle(readEvent_);
        readEvent_ = nullptr;
    }
#endif
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

    if(pipe_ == INVALID_HANDLE_VALUE || !readEvent_)
        return fail(ERROR_INVALID_HANDLE, 0, nullptr);

    ULONGLONG deadline = GetTickCount64() + kEnqueuedAckTimeoutMs;
    bool enqueuedObserved = false;
    while(GetTickCount64() <= deadline)
    {
        OrionInputAck ack{};
        DWORD read = 0;
        DWORD readError = ERROR_SUCCESS;
        OVERLAPPED ov{};
        ov.hEvent = readEvent_;
        ResetEvent(readEvent_);
        bool readOk = false;
        if(ReadFile(pipe_, &ack, sizeof(ack), nullptr, &ov))
        {
            if(GetOverlappedResult(pipe_, &ov, &read, FALSE))
            {
                readOk = read == sizeof(ack);
                if(!readOk)
                    readError = ERROR_INVALID_DATA;
            }
            else
            {
                readError = GetLastError();
            }
        }
        else
        {
            readError = GetLastError();
            if(readError == ERROR_IO_PENDING)
            {
                const ULONGLONG now = GetTickCount64();
                const DWORD remaining = now >= deadline
                    ? 0 : static_cast<DWORD>(deadline - now);
                const DWORD waitResult = WaitForSingleObject(readEvent_, remaining);
                if(waitResult == WAIT_OBJECT_0)
                {
                    if(GetOverlappedResult(pipe_, &ov, &read, FALSE))
                    {
                        readOk = read == sizeof(ack);
                        readError = readOk ? ERROR_SUCCESS : ERROR_INVALID_DATA;
                    }
                    else
                    {
                        readError = GetLastError();
                    }
                }
                else
                {
                    const DWORD waitError = waitResult == WAIT_TIMEOUT
                        ? ERROR_TIMEOUT : GetLastError();
                    // Drain cancellation before the stack ACK buffer goes away. If completion won
                    // the timeout/cancel race, GetOverlappedResult still returns the exact ACK and
                    // it is safe to validate it. A genuinely canceled/failed read remains ambiguous.
                    CancelIoEx(pipe_, &ov);
                    if(WaitForSingleObject(readEvent_, INFINITE) == WAIT_OBJECT_0
                        && GetOverlappedResult(pipe_, &ov, &read, FALSE))
                    {
                        readOk = read == sizeof(ack);
                        readError = readOk ? ERROR_SUCCESS : ERROR_INVALID_DATA;
                    }
                    else
                    {
                        const DWORD completionError = GetLastError();
                        readError = completionError == ERROR_OPERATION_ABORTED
                            ? waitError : completionError;
                    }
                }
            }
            else
            {
                readOk = false;
            }
        }

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
    if(!writeEvent_ || !readEvent_)  // bounded duplex completion unavailable -> fail closed
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
        return InputRouteWriteResult::Unchanged;

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
        || pipe_ == INVALID_HANDLE_VALUE || !writeEvent_ || !readEvent_
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
    if(pipe_ == INVALID_HANDLE_VALUE || !writeEvent_ || !readEvent_)
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
    lastTransactionTiming_.begin(p.seq);
    lastWriteUs_.store(0, std::memory_order_relaxed);
    DWORD written = 0;
    LARGE_INTEGER start{}, end{};
    const BOOL startOk = QueryPerformanceCounter(&start);

    // Overlapped write with a bounded timeout: the caller is the TIME_CRITICAL fire thread, so a wedged
    // reader must never block it. Normal writes complete in microseconds. If an operation may have been
    // accepted but does not finish inside the budget, the route becomes ambiguous: close the pipe, keep
    // ViGEm neutral, and let OrionStream's duplex-loss fence stop the session. `p` outlives every path
    // below because cancellation is drained before return, avoiding async use-after-scope.
    OVERLAPPED ov{};
    ov.hEvent = writeEvent_;
    ResetEvent(writeEvent_);
    bool writeOk = false;
    bool writeMayHaveBeenAccepted = false;
    if(WriteFile(pipe_, &p, sizeof(p), nullptr, &ov))
    {
        writeMayHaveBeenAccepted = true;
        writeOk = GetOverlappedResult(pipe_, &ov, &written, FALSE) && written == sizeof(p);
    }
    else if(GetLastError() == ERROR_IO_PENDING)
    {
        writeMayHaveBeenAccepted = true;
        if(WaitForSingleObject(writeEvent_, kWriteTimeoutMs) == WAIT_OBJECT_0)
        {
            writeOk = GetOverlappedResult(pipe_, &ov, &written, FALSE) && written == sizeof(p);
        }
        else
        {
            // Reader stalled past the budget -> cancel and DRAIN (blocks until the op stops touching
            // &p). Cancellation can lose the exact-boundary race to a successful write. Once the
            // event is signaled, query the terminal result: only an exact completed packet is accepted;
            // ERROR_OPERATION_ABORTED and every partial/failed completion remain ambiguous/fail-closed.
            CancelIoEx(pipe_, &ov);
            if(WaitForSingleObject(writeEvent_, INFINITE) == WAIT_OBJECT_0)
                writeOk = GetOverlappedResult(pipe_, &ov, &written, FALSE)
                    && written == sizeof(p);
        }
    }
    // else: immediate failure (not pending) -> writeOk stays false.

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
            // Closing the duplex client makes OrionStream terminate the ambiguous session.
            closePipe();
        }
    }
    return result;
}
#else
void OrionInputClient::closePipe() {}
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
