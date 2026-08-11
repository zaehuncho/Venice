#include "MeterDelayIntercept.h"

#include "WinDivertApi.h"

// Reuse the app's high-resolution waitable-timer idiom for the flush loop's
// coarse/fine deadline split instead of re-deriving it (wave-1 hazard 5). It
// lives in native_orion/src, which orion_apply_target_defaults puts on the
// include path.
#include "PreciseWaitTimer.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <stdexcept>

namespace venicenet {

namespace {

double qpcSeconds()
{
    static const double freq = [] {
        LARGE_INTEGER f;
        QueryPerformanceFrequency(&f);
        return static_cast<double>(f.QuadPart);
    }();
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return static_cast<double>(c.QuadPart) / freq;
}

// Parse a strict dotted-quad into four octets. Rejects anything that is not
// exactly four decimal octets 0-255 (digits only). Returns false on rejection.
bool parseIpv4(const std::string& value, int octets[4])
{
    int count = 0;
    size_t pos = 0;
    int dots = 0;
    for (const char c : value) {
        if (c == '.') ++dots;
    }
    if (dots != 3) {
        return false;
    }
    while (count < 4) {
        const size_t dot = value.find('.', pos);
        const std::string piece =
            (dot == std::string::npos) ? value.substr(pos) : value.substr(pos, dot - pos);
        if (piece.empty() || piece.size() > 3) {
            return false;
        }
        int v = 0;
        for (const char ch : piece) {
            if (ch < '0' || ch > '9') {
                return false;
            }
            v = v * 10 + (ch - '0');
        }
        if (v > 255) {
            return false;
        }
        octets[count++] = v;
        if (dot == std::string::npos) {
            break;
        }
        pos = dot + 1;
    }
    return count == 4;
}

} // namespace

MeterDelayIntercept::MeterDelayIntercept(double slewMsPerS, Clock clock)
    : clock_(clock ? std::move(clock) : Clock(&qpcSeconds))
    , slewMsPerS_(std::max(0.0, slewMsPerS))
{
    stats_.slewCapMsPerS = slewMsPerS_;
}

MeterDelayIntercept::~MeterDelayIntercept()
{
    stop("destruct");
}

void MeterDelayIntercept::setSendSink(SendSink sink)
{
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    sink_ = std::move(sink);
}

double MeterDelayIntercept::now() const
{
    return clock_();
}

// ── Introspection ──────────────────────────────────────────────────────────

int MeterDelayIntercept::bufferDepth() const
{
    return depth_.load();
}

MeterDelayIntercept::Snapshot MeterDelayIntercept::snapshot() const
{
    // Lock-free, like nexus_svc.py's snapshot(): reads the atomic mirrors so the
    // high-rate telemetry path never contends with the ~200Hz flush loop.
    const double applied = delayMs_.load();
    const double target = targetMs_.load();
    Snapshot s;
    s.active = running_.load();
    s.delayMs = std::round(applied * 1000.0) / 1000.0;
    s.targetMs = std::round(target * 1000.0) / 1000.0;
    s.settled = std::abs(applied - target) <= 0.5;
    s.bufferDepth = depth_.load();
    return s;
}

MeterDelayIntercept::Stats MeterDelayIntercept::stats() const
{
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    Stats out = stats_;
    out.peakSlewMsPerS = std::round(peakSlewMsPerS_.load() * 1000.0) / 1000.0;
    out.slewCapMsPerS = slewMsPerS_;
    return out;
}

std::string MeterDelayIntercept::filter() const
{
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    return filter_;
}

// ── Delay control ──────────────────────────────────────────────────────────

double MeterDelayIntercept::setDelay(double delayMs, bool touch)
{
    if (!std::isfinite(delayMs)) {
        throw std::invalid_argument("invalid_delay_ms");
    }
    double value = std::max(0.0, std::min(kHardMaxMs, delayMs));

    const double n = now();
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    if (touch) {
        lastCmdTs_ = n;
    }
    targetMs_.store(value);
    if (slewMsPerS_ <= 0.0) {
        // Limiter disabled (tests): target and applied are the same thing.
        advanceSlewLocked(n);
    }
    return value;
}

void MeterDelayIntercept::forceZero(const std::string& reason)
{
    const double n = now();
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        if (delayMs_.load() <= 0.0 && targetMs_.load() <= 0.0) {
            return;
        }
        ++stats_.emergencyZero;
        targetMs_.store(0.0);
        delayMs_.store(0.0);
        lastSlewTs_ = n;
        rescheduleLocked(n, 0.0);
    }
    if (log_) {
        log_("meter delay forced to 0 (" + reason + ")");
    }
}

double MeterDelayIntercept::advanceSlew(double nowArg)
{
    const double n = (nowArg < 0.0) ? now() : nowArg;
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    return advanceSlewLocked(n);
}

double MeterDelayIntercept::advanceSlewLocked(double n)
{
    const double target = targetMs_.load();
    const double applied = delayMs_.load();
    const double delta = target - applied;
    if (std::abs(delta) <= 1e-9) {
        // SNAP, do not merely return. perf_counter values are ~1e4, so a 5ms step
        // carries ~5e-13 of float error; left un-snapped on the way DOWN the
        // residue is permanent and poisonous — _delay_ms stays fractionally
        // above 0 so enqueue()'s zero-delay fast path never fires again and every
        // packet is buffered forever. Ported verbatim (nexus_svc.py:1082-1095).
        delayMs_.store(target);
        lastSlewTs_ = n;
        return target;
    }

    if (slewMsPerS_ <= 0.0) {
        // Limiter disabled (tests only). Apply instantly.
        const double previous = applied;
        delayMs_.store(target);
        lastSlewTs_ = n;
        if (target < previous) {
            rescheduleLocked(n, target / 1000.0);
        }
        return target;
    }

    double elapsed = n - lastSlewTs_;
    if (elapsed <= 0.0) {
        return applied;
    }
    // Token bucket: a long quiet period must not bank an unbounded step.
    elapsed = std::min(elapsed, kSlewMaxAccumS);
    const double maxStep = slewMsPerS_ * elapsed;

    const double previous = applied;
    double next;
    if (std::abs(delta) > maxStep) {
        ++stats_.slewLimitedSteps;
        next = previous + std::copysign(maxStep, delta);
    } else {
        next = target;
    }
    delayMs_.store(next);
    lastSlewTs_ = n;

    const double appliedDelta = std::abs(next - previous);
    if (elapsed > 0.0) {
        const double rate = appliedDelta / elapsed;
        if (rate > peakSlewMsPerS_.load()) {
            peakSlewMsPerS_.store(rate);
        }
    }
    if (next < previous) {
        rescheduleLocked(n, next / 1000.0);
    }
    return next;
}

void MeterDelayIntercept::rescheduleLocked(double n, double delayS)
{
    if (buffer_.empty()) {
        return;
    }
    std::deque<Entry> rescheduled;
    double prevT = n - kDrainMinSpacingS;
    for (auto& entry : buffer_) {
        double t = std::min(entry.releaseAt, entry.arrivalAt + delayS);
        t = std::max(t, prevT + kDrainMinSpacingS);
        // Never push a packet later than it was already scheduled for, and never
        // let an overdue entry drag the pacing anchor into the past.
        t = std::min(t, std::max(entry.releaseAt, n));
        prevT = t;
        Entry moved;
        moved.releaseAt = t;
        moved.arrivalAt = entry.arrivalAt;
        moved.pkt = std::move(entry.pkt);
        rescheduled.push_back(std::move(moved));
    }
    buffer_ = std::move(rescheduled);
}

// ── Packet path ──────────────────────────────────────────────────────────

bool MeterDelayIntercept::sendLocked(const Packet& pkt)
{
    if (!sink_) {
        return false; // equivalent to nexus_svc's "handle is None"
    }
    bool ok = false;
    try {
        ok = sink_(pkt);
    } catch (...) {
        ok = false;
    }
    if (!ok) {
        ++stats_.sendErrors;
        throttledWarn("meter intercept send failed");
        return false;
    }
    ++stats_.sent;
    return true;
}

void MeterDelayIntercept::throttledWarn(const std::string& line)
{
    const double n = now();
    if (n - logThrottleTs_ < 1.0) {
        return;
    }
    logThrottleTs_ = n;
    if (log_) {
        log_(line);
    }
}

void MeterDelayIntercept::enqueue(Packet pkt)
{
    const double n = now();
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    ++stats_.intercepted;
    const double delayS = delayMs_.load() / 1000.0;

    // [ORION_METER_PACING 2026-08-08] Arrival-cadence EWMA, updated for EVERY
    // intercepted packet (fast path included) so the estimate is already warm
    // when a delay engages. An idle gap (dead ball, load screen) resets it: the
    // burst after a gap has a new phase and must not be "caught up" against the
    // old one.
    if (lastArrivalAt_ >= 0.0 && n > lastArrivalAt_) {
        const double dt = n - lastArrivalAt_;
        if (dt >= kPaceGapResetS) {
            arrivalIntervalEwmaS_ = 0.0;
        } else if (arrivalIntervalEwmaS_ <= 0.0) {
            arrivalIntervalEwmaS_ = dt;
        } else {
            arrivalIntervalEwmaS_ =
                kPaceAlpha * dt + (1.0 - kPaceAlpha) * arrivalIntervalEwmaS_;
        }
    }
    lastArrivalAt_ = n;

    if (delayS <= 0.0 && buffer_.empty()) {
        // Bug A: the fast path is only safe when the buffer is provably empty
        // under the same lock that serialises sends.
        ++stats_.passed;
        sendLocked(pkt);
        lastScheduledReleaseAt_ = n;
        return;
    }

    // Nominal (arrival-shaped) schedule: exactly the commanded delay.
    const double nominal = n + delayS;
    double releaseAt = nominal;

    // [ORION_METER_PACING 2026-08-08] Pace the release onto the flow's own
    // average cadence instead of copying per-packet arrival jitter through the
    // buffer. Hard-bounded: the paced time may never deviate from nominal by
    // more than kPaceMaxShiftS, so the effective per-packet delay stays within
    // commanded ± 8 ms and the clamp re-anchors the schedule to arrival truth
    // on every packet (no cumulative drift). The anchor must extend into
    // now-or-later — a stale anchor (scheduling gap) means the cadence chain is
    // broken and nominal is the honest choice.
    if (pacingEnabled_ && arrivalIntervalEwmaS_ >= kPaceMinIntervalS) {
        const double base =
            buffer_.empty() ? lastScheduledReleaseAt_ : buffer_.back().releaseAt;
        if (base >= 0.0 && base + arrivalIntervalEwmaS_ >= n) {
            const double chained = base + arrivalIntervalEwmaS_;
            // Servo toward nominal (see kPaceCorrectionGain): kills the chain's
            // cumulative drift so the hard clamp below stays a guard rail, not
            // the operating point.
            const double paced =
                chained + kPaceCorrectionGain * (nominal - chained);
            releaseAt = std::clamp(paced, nominal - kPaceMaxShiftS,
                                   nominal + kPaceMaxShiftS);
        }
    }

    if (!buffer_.empty()) {
        // Keep release times monotonic so FIFO order is structural.
        releaseAt = std::max(releaseAt, buffer_.back().releaseAt);
    }
    releaseAt = std::max(releaseAt, n);

    if (static_cast<int>(buffer_.size()) >= kBufferMaxPackets) {
        // Bug C overflow policy: release the OLDEST early, never drop.
        const int over = static_cast<int>(buffer_.size()) - kBufferMaxPackets + 1;
        for (int i = 0; i < over; ++i) {
            Packet old = std::move(buffer_.front().pkt);
            buffer_.pop_front();
            ++stats_.overflowReleased;
            sendLocked(old);
        }
        throttledWarn("meter buffer cap reached — early-released oldest packet(s)");
    }

    Entry entry;
    entry.releaseAt = releaseAt;
    entry.arrivalAt = n;
    entry.pkt = std::move(pkt);
    buffer_.push_back(std::move(entry));
    depth_.store(static_cast<int>(buffer_.size()));
    lastScheduledReleaseAt_ = releaseAt;
    ++stats_.delayed;
}

void MeterDelayIntercept::setReleasePacing(bool enabled)
{
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    pacingEnabled_ = enabled;
}

bool MeterDelayIntercept::releasePacing() const
{
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    return pacingEnabled_;
}

int MeterDelayIntercept::flushDue(double nowArg)
{
    const double n = (nowArg < 0.0) ? now() : nowArg;
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    int sent = 0;
    while (!buffer_.empty() && buffer_.front().releaseAt <= n) {
        Packet pkt = std::move(buffer_.front().pkt);
        buffer_.pop_front();
        sendLocked(pkt);
        ++sent;
    }
    depth_.store(static_cast<int>(buffer_.size()));
    return sent;
}

bool MeterDelayIntercept::nextReleaseAt(double* out) const
{
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    if (buffer_.empty()) {
        return false;
    }
    if (out) {
        *out = buffer_.front().releaseAt;
    }
    return true;
}

// ── Watchdog ────────────────────────────────────────────────────────────

bool MeterDelayIntercept::watchdogCheck(double nowArg)
{
    const double n = (nowArg < 0.0) ? now() : nowArg;
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        if (!running_.load()) {
            return false;
        }
        // Either an applied delay or a standing non-zero target counts as armed.
        if (delayMs_.load() <= 0.0 && targetMs_.load() <= 0.0) {
            return false;
        }
        if ((n - lastCmdTs_) <= kWatchdogTimeoutS) {
            return false;
        }
        ++stats_.watchdogTrips;
    }
    forceZero("command_starvation");
    broadcastState("watchdog_zero", "command_starvation");
    return true;
}

void MeterDelayIntercept::broadcastState(const std::string& state, const std::string& reason)
{
    if (broadcaster_) {
        try {
            broadcaster_(state, reason);
        } catch (...) {
        }
    }
}

// ── Filter / IP helpers ─────────────────────────────────────────────────

std::string MeterDelayIntercept::validatedIpv4(const std::string& value, bool requirePublic)
{
    int o[4];
    if (!parseIpv4(value, o)) {
        return "";
    }
    const unsigned first = static_cast<unsigned>(o[0]);
    const unsigned second = static_cast<unsigned>(o[1]);
    const unsigned third = static_cast<unsigned>(o[2]);
    // Never accept unspecified / loopback / multicast / reserved (mirrors
    // ipaddress is_unspecified / is_loopback / is_multicast / is_reserved).
    const bool unspecified = (o[0] == 0 && o[1] == 0 && o[2] == 0 && o[3] == 0);
    const bool loopback = (first == 127U);
    const bool multicast = (first >= 224U && first <= 239U);
    const bool reserved = (first >= 240U); // 240.0.0.0/4 (includes 255.255.255.255)
    if (unspecified || loopback || multicast || reserved) {
        return "";
    }
    if (requirePublic) {
        // is_global: reject private and every special-use range that is not a
        // globally-routable unicast address. Keeps LAN<->LAN Remote Play out.
        const bool isPrivate = first == 10U || (first == 172U && second >= 16U && second <= 31U)
            || (first == 192U && second == 168U);
        const bool special = first == 0U || (first == 100U && second >= 64U && second <= 127U)
            || (first == 169U && second == 254U) || (first == 192U && second == 0U && third == 0U)
            || (first == 192U && second == 0U && third == 2U)
            || (first == 192U && second == 88U && third == 99U)
            || (first == 198U && (second == 18U || second == 19U))
            || (first == 198U && second == 51U && third == 100U)
            || (first == 203U && second == 0U && third == 113U);
        if (isPrivate || special) {
            return "";
        }
    }
    // Canonical dotted quad (no leading zeros, only digits+dots) — safe to splice
    // into a WinDivert filter expression.
    return std::to_string(o[0]) + "." + std::to_string(o[1]) + "." + std::to_string(o[2]) + "."
        + std::to_string(o[3]);
}

std::string MeterDelayIntercept::buildInterceptFilter(const std::string& consoleIp,
                                                      const std::string& courtIp)
{
    // Every clause is load-bearing — see nexus_svc.py _build_intercept_filter:
    //   ip and udp                  : the game flow is IPv4 UDP (excludes probes)
    //   ip.SrcAddr == court         : pins the PUBLIC 2K court server
    //   ip.DstAddr == console       : the inbound direction (server -> console)
    //   udp.SrcPort/DstPort in range: the NBA 2K gameplay port range
    // NETWORK_FORWARD + a public SrcAddr structurally exclude Remote Play.
    return std::string("ip and udp") + " and ip.SrcAddr == " + courtIp + " and ip.DstAddr == "
        + consoleIp + " and ((udp.SrcPort >= " + std::to_string(kPortMin) + " and udp.SrcPort <= "
        + std::to_string(kPortMax) + ") or (udp.DstPort >= " + std::to_string(kPortMin)
        + " and udp.DstPort <= " + std::to_string(kPortMax) + "))";
}

// ── Lifecycle (WinDivert handle + threads — service only) ─────────────────

std::pair<bool, std::string> MeterDelayIntercept::start(const std::string& consoleIp,
                                                        const std::string& courtIp)
{
    const std::string console = validatedIpv4(consoleIp);
    const std::string court = validatedIpv4(courtIp, /*requirePublic=*/true);
    if (console.empty() || court.empty()) {
        return {false, "intercept_requires_ips"};
    }
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        if (running_.load()) {
            if (console == consoleIp_ && court == courtIp_) {
                return {true, ""};
            }
            return {false, "intercept_already_running"};
        }
        if (starting_) {
            return {false, "intercept_already_running"};
        }
        starting_ = true;
    }

    const std::string filterStr = buildInterceptFilter(console, court);
    void* handle = nullptr;
    std::string openErr;

    if (!lib_ || !lib_->loaded()) {
        openErr = "pydivert not available";
    } else {
        // Validate the filter first (WinDivertHelperCompileFilter), then open a
        // non-SNIFF NETWORK_FORWARD handle: packets are removed from the stack
        // and only reach the console when we re-inject them.
        char object[256];
        const char* errStr = nullptr;
        UINT errPos = 0;
        if (!lib_->compileFilter(filterStr.c_str(), WINDIVERT_LAYER_NETWORK_FORWARD, object,
                                 sizeof(object), &errStr, &errPos)) {
            openErr = std::string("bad filter: ") + (errStr ? errStr : "unknown");
        } else {
            handle = lib_->open(filterStr.c_str(), WINDIVERT_LAYER_NETWORK_FORWARD, /*priority=*/0,
                                /*flags=*/0);
            if (handle == INVALID_HANDLE_VALUE || handle == nullptr) {
                handle = nullptr;
                openErr = "WinDivertOpen failed (GetLastError="
                    + std::to_string(static_cast<unsigned>(::GetLastError())) + ")";
            }
        }
    }

    if (handle == nullptr) {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        starting_ = false;
        if (log_) {
            log_("meter intercept open failed: " + openErr);
        }
        return {false, "intercept_open_failed: " + openErr};
    }

    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        starting_ = false;
        handle_ = handle;
        consoleIp_ = console;
        courtIp_ = court;
        filter_ = filterStr;
        buffer_.clear();
        depth_.store(0);
        // [ORION_METER_PACING] Fresh session, fresh cadence estimate — a stale
        // EWMA/anchor from the previous game must not pace the new flow.
        lastArrivalAt_ = -1.0;
        arrivalIntervalEwmaS_ = 0.0;
        lastScheduledReleaseAt_ = -1.0;
        // Always come up at zero delay: a stale non-zero value must never be
        // applied implicitly by opening the handle.
        delayMs_.store(0.0);
        targetMs_.store(0.0);
        const double n = now();
        lastSlewTs_ = n;
        lastCmdTs_ = n;
        peakSlewMsPerS_.store(0.0);
        // The real send sink replays captured packets on this handle.
        sink_ = [this](const Packet& pkt) { return this->winDivertSend(pkt); };
        stopFlag_.store(false);
        stopEvent_ = ::CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!stopEvent_ && log_) {
            // Both worker loops use this handle as their ONLY sleep. They now fall back
            // to a plain sleep (see watchdogLoop / flushLoop), so a failure here costs
            // stop latency rather than pinning a core -- but it must not be silent.
            log_("meter delay: CreateEventW failed; workers fall back to timed sleep");
        }
        running_.store(true);
        captureThread_ = std::thread(&MeterDelayIntercept::captureLoop, this);
        flushThread_ = std::thread(&MeterDelayIntercept::flushLoop, this);
        watchdogThread_ = std::thread(&MeterDelayIntercept::watchdogLoop, this);
    }
    if (log_) {
        log_("meter intercept opened, filter: " + filterStr);
    }
    broadcastState("started", "client_request");
    return {true, ""};
}

bool MeterDelayIntercept::stop(const std::string& reason)
{
    void* handle = nullptr;
    void* stopEvent = nullptr;
    std::thread captureThread;
    std::thread flushThread;
    std::thread watchdogThread;
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        if (!running_.load()) {
            return false;
        }
        running_.store(false);
        handle = handle_;
        stopEvent = stopEvent_;
        captureThread = std::move(captureThread_);
        flushThread = std::move(flushThread_);
        watchdogThread = std::move(watchdogThread_);
    }
    stopFlag_.store(true);
    if (stopEvent) {
        ::SetEvent(static_cast<HANDLE>(stopEvent)); // wake the flush timer
    }
    // Unblock the capture thread's blocking recv() by shutting down + closing the
    // handle (closing a handle is what unblocks WinDivertRecv).
    if (handle && lib_ && lib_->shutdown) {
        lib_->shutdown(handle, WINDIVERT_SHUTDOWN_RECV);
    }

    const std::thread::id self = std::this_thread::get_id();

    // Let the flush thread retire first so nothing double-sends during the drain.
    if (flushThread.joinable()) {
        if (flushThread.get_id() != self) {
            flushThread.join();
        } else {
            flushThread.detach();
        }
    }

    std::deque<Entry> pending;
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        pending = std::move(buffer_);
        buffer_.clear();
        depth_.store(0);
        delayMs_.store(0.0);
        targetMs_.store(0.0);
        for (auto& entry : pending) {
            sendLocked(entry.pkt);
        }
        handle_ = nullptr;
    }
    if (handle && lib_ && lib_->close) {
        lib_->close(handle);
    }
    // Now the capture thread's recv() has been unblocked; join the rest.
    if (captureThread.joinable()) {
        if (captureThread.get_id() != self) {
            captureThread.join();
        } else {
            captureThread.detach();
        }
    }
    if (watchdogThread.joinable()) {
        if (watchdogThread.get_id() != self) {
            watchdogThread.join();
        } else {
            watchdogThread.detach();
        }
    }
    if (stopEvent) {
        ::CloseHandle(static_cast<HANDLE>(stopEvent));
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        if (stopEvent_ == stopEvent) {
            stopEvent_ = nullptr;
        }
    }
    if (log_) {
        log_("meter intercept stopped (" + reason + "), flushed "
             + std::to_string(pending.size()) + " buffered packet(s)");
    }
    broadcastState("stopped", reason);
    return true;
}

bool MeterDelayIntercept::deadManStop(const std::string& reason)
{
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        if (!running_.load()) {
            return false;
        }
        ++stats_.deadManTrips;
    }
    if (log_) {
        log_("meter intercept dead-man switch: " + reason + " -> forcing delay 0");
    }
    forceZero("dead_man");
    return stop(reason);
}

void MeterDelayIntercept::retarget(const std::string& consoleIp, const std::string& courtIp)
{
    const std::string console = validatedIpv4(consoleIp);
    const std::string court = validatedIpv4(courtIp, /*requirePublic=*/true);
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        if (!running_.load()) {
            return;
        }
        if (!console.empty() && !court.empty() && console == consoleIp_ && court == courtIp_) {
            return;
        }
    }
    stop("ip_changed");
    if (!console.empty() && !court.empty()) {
        start(console, court);
    }
}

// ── Private WinDivert send (real path) ────────────────────────────────────

bool MeterDelayIntercept::winDivertSend(const Packet& pkt)
{
    // Called under mutex_ (from sendLocked). handle_ only changes under the lock.
    if (!lib_ || !lib_->send || handle_ == nullptr) {
        return false;
    }
    UINT sendLen = 0;
    const BOOL ok = lib_->send(handle_, pkt.data.data(), static_cast<UINT>(pkt.data.size()),
                               &sendLen, pkt.addr.data());
    return ok != FALSE;
}

void MeterDelayIntercept::captureLoop()
{
    std::vector<std::uint8_t> buf(0xFFFF);
    while (!stopFlag_.load()) {
        void* handle = nullptr;
        {
            std::lock_guard<std::recursive_mutex> lock(mutex_);
            handle = handle_;
        }
        if (handle == nullptr) {
            break;
        }
        UINT recvLen = 0;
        Packet pkt;
        pkt.addr = {};
        const BOOL ok = lib_->recv(handle, buf.data(), static_cast<UINT>(buf.size()), &recvLen,
                                   pkt.addr.data());
        if (!ok) {
            if (stopFlag_.load()) {
                break;
            }
            if (log_) {
                log_("meter intercept recv failed — stopping intercept");
            }
            // Spawn stop on a detached thread so recv-error teardown does not run
            // inside the capture thread it must join.
            std::thread([this] { this->stop("recv_error"); }).detach();
            break;
        }
        pkt.data.assign(buf.begin(), buf.begin() + recvLen);
        if (stopFlag_.load()) {
            // Teardown in progress but stop()'s final drain may not have run yet.
            // Park on the tail so this packet is released LAST, never ahead of the
            // drain (nexus_svc.py:1243-1255).
            std::lock_guard<std::recursive_mutex> lock(mutex_);
            if (!buffer_.empty()) {
                Entry entry;
                entry.releaseAt = buffer_.back().releaseAt;
                entry.arrivalAt = now();
                entry.pkt = std::move(pkt);
                buffer_.push_back(std::move(entry));
                depth_.store(static_cast<int>(buffer_.size()));
            } else {
                sendLocked(pkt);
            }
            break;
        }
        enqueue(std::move(pkt));
    }
}

void MeterDelayIntercept::flushLoop()
{
    orion::PreciseWaitTimer timer;
    const bool haveTimer = timer.create();
    HANDLE wake = static_cast<HANDLE>(stopEvent_);

    const auto waitFor = [&](double seconds) {
        if (seconds <= 0.0) {
            return;
        }
        const auto target = std::chrono::steady_clock::now()
            + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                std::chrono::duration<double>(seconds));
        if (haveTimer) {
            timer.waitUntil(target, wake);
        } else if (wake) {
            ::WaitForSingleObject(wake, static_cast<DWORD>(seconds * 1000.0 + 0.5));
        } else {
            // Same hazard as watchdogLoop: with neither a precise timer nor the event,
            // waitFor was a no-op and the flush loop span. Sleep unconditionally.
            std::this_thread::sleep_for(std::chrono::duration<double>(seconds));
        }
    };

    while (!stopFlag_.load()) {
        // The slew advance MUST run before the flush: it makes the applied delay
        // a smooth function of wall time, and flush_due() reads the schedule it
        // rewrites.
        advanceSlew();
        flushDue();
        double nxt = 0.0;
        if (!nextReleaseAt(&nxt)) {
            waitFor(kFlushIdleS);
            continue;
        }
        const double remaining = nxt - now();
        if (remaining <= 0.0) {
            continue;
        }
        if (remaining > kFlushSlackS + kFlushFineTickS) {
            waitFor(std::min(remaining - kFlushSlackS, kFlushCoarseMaxS));
        } else {
            waitFor(kFlushFineTickS);
        }
    }
}

void MeterDelayIntercept::watchdogLoop()
{
    HANDLE wake = static_cast<HANDLE>(stopEvent_);
    while (!stopFlag_.load()) {
        if (wake) {
            if (::WaitForSingleObject(wake, static_cast<DWORD>(kWatchdogTickS * 1000.0))
                == WAIT_OBJECT_0) {
                break;
            }
        } else {
            // This wait is the loop's ONLY sleep. Without a fallback, a null stopEvent_
            // turns the whole loop into a tight spin running watchdogCheck() at MHz
            // rates inside a LocalSystem service -- one pinned core for as long as the
            // intercept is up.
            std::this_thread::sleep_for(std::chrono::duration<double>(kWatchdogTickS));
        }
        if (stopFlag_.load()) {
            break;
        }
        const bool starved = watchdogCheck();
        if (starved && log_) {
            log_("meter delay watchdog: no set_meter_delay in >500ms — forcing delay 0");
        }
    }
}

} // namespace venicenet
