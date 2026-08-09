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

uint64_t writeElapsedUs(const LARGE_INTEGER& start, const LARGE_INTEGER& end)
{
    static const LARGE_INTEGER freq = [] {
        LARGE_INTEGER f{};
        QueryPerformanceFrequency(&f);
        return f;
    }();
    if(freq.QuadPart <= 0)
        return 0;
    return static_cast<uint64_t>(((end.QuadPart - start.QuadPart) * 1000000LL) / freq.QuadPart);
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
    LARGE_INTEGER ackStart{};
    QueryPerformanceCounter(&ackStart);
    OrionInputAck lastObservedAck{};
    bool haveObservedAck = false;

    const auto recordDiagnostics = [&](DWORD winError, DWORD bytes,
                                       const OrionInputAck* ack) {
        LARGE_INTEGER ackEnd{};
        QueryPerformanceCounter(&ackEnd);
        lastAckWinError_.store(winError, std::memory_order_relaxed);
        lastAckExpectedSeq_.store(sourceSeq, std::memory_order_relaxed);
        lastAckReadBytes_.store(bytes, std::memory_order_relaxed);
        lastAckSourceSeq_.store(ack ? ack->sourceSeq : 0, std::memory_order_relaxed);
        lastAckStage_.store(ack ? ack->stage : 0, std::memory_order_relaxed);
        lastAckProtocolError_.store(ack ? ack->error : 0, std::memory_order_relaxed);
        lastAckWaitUs_.store(writeElapsedUs(ackStart, ackEnd), std::memory_order_relaxed);
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
    const ControllerState& state, bool own, bool forceWrite)
{
    std::lock_guard<std::mutex> lock(ioMutex_);
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
    if(!forceWrite && haveLast_ && sameInput(last_, p))
        return InputRouteWriteResult::Unchanged;

    p.seq = ++seq_;
    DWORD written = 0;
    LARGE_INTEGER start{}, end{};
    QueryPerformanceCounter(&start);

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

    QueryPerformanceCounter(&end);
    const uint64_t elapsedUs = writeElapsedUs(start, end);
    lastWriteUs_.store(elapsedUs, std::memory_order_relaxed);
    updateMax(maxWriteUs_, elapsedUs);
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
            return result;
        }
    }
    last_ = p;
    haveLast_ = true;
    return result;
}
#else
void OrionInputClient::closePipe() {}
bool OrionInputClient::ensureConnected() { return false; }
bool OrionInputClient::send(const ControllerState&, bool) { return false; }
InputRouteWriteResult OrionInputClient::sendDetailed(const ControllerState&, bool, bool)
{
    std::lock_guard<std::mutex> lock(ioMutex_);
    return InputRouteWriteResult::Failed;
}
#endif

} // namespace orion
