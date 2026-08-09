#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  MeterDelayIntercept.h — C++ port of nexus_svc.py's _InboundDelayBuffer
// ───────────────────────────────────────────────────────────────────────────
//
//  Holds inbound game packets (public court server -> console) on a second,
//  independent WinDivert NETWORK_FORWARD handle and re-injects them late, with a
//  structurally-capped slew so a changing delay can never stutter the game flow.
//  Every design decision, constant and invariant is a direct port of
//  nexus_svc.py's extensively-commented _InboundDelayBuffer (the reference
//  implementation that stays in-tree until wave 3-lite). See that class for the
//  derivations; the porting notes below only flag what changed in C++.
//
//  Ordering contract   — packets leave in arrival order, unconditionally:
//    (1) release_at is monotonically non-decreasing across the buffer, so a
//        flush pass releases a strict prefix;
//    (2) the zero-delay fast path fires ONLY when the buffer is provably empty
//        under the same lock that serialises sends.
//
//  Slew contract       — setDelay() sets a TARGET; the flush loop advances the
//    applied delay toward it at <= kMaxSlewMsPerS with a token-bucket burst cap
//    (kSlewMaxAccumS). The float-residue SNAP-TO-TARGET in advanceSlewLocked()
//    is required: without it the applied delay lands a hair above 0 on the way
//    down and the zero-delay fast path never re-fires (every packet buffered
//    forever). Ported verbatim from nexus_svc.py:1082-1095.
//
//  Safety contract     — a stuck non-zero delay is a broken console. forceZero()
//    is the only path allowed to bypass the slew cap (stop / dead-man /
//    watchdog); even then the backlog is re-paced by rescheduleLocked(), never
//    bursted onto the wire.
//
//  Locking             — a single mutex guards the buffer, the delay values AND
//    the sends. Serialising sends under that lock is what makes the ordering
//    contract provable. recv() is the only WinDivert call made outside it.
//
//  TESTABILITY: the pure delay-queue logic (setDelay / enqueue / flushDue /
//  advanceSlew / forceZero / watchdogCheck / reschedule / overflow) takes an
//  injectable monotonic clock and an injectable send sink, so unit tests drive
//  it with a virtual clock and a recording sink and NEVER touch WinDivert. The
//  start()/stop() lifecycle (WinDivert handle + capture/flush/watchdog threads)
//  is compiled but only ever exercised inside the LocalSystem service.

#include <array>
#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace venicenet {

class WinDivertLibrary;

class MeterDelayIntercept {
public:
    // Informational bands (the controller's business); the service enforces only
    // the hard clamp and the slew cap.
    static constexpr double kAdaptiveMinMs = 150.0;
    static constexpr double kAdaptiveMaxMs = 170.0;
    static constexpr double kManualMinMs = 100.0;
    // [ORION_METER_DELAY_RANGE 2026-08-08] Raised in lockstep with the app's
    // 100-600 ms slider band (AppConfigData::kMeterDelay{Min,Max}Ms) and
    // VENICENET_MAX_DELAY_MS. Takes effect at the next service (re)start.
    static constexpr double kManualMaxMs = 600.0;
    static constexpr double kHardMaxMs = 600.0; // _METER_DELAY_HARD_MAX_MS

    static constexpr double kMaxSlewMsPerS = 100.0;   // _METER_MAX_SLEW_MS_PER_S
    static constexpr double kSlewMaxAccumS = 0.100;   // _METER_SLEW_MAX_ACCUM_S

    static constexpr int kPortMin = 30000;            // _METER_PORT_MIN
    static constexpr int kPortMax = 30020;            // _METER_PORT_MAX

    static constexpr int kBufferMaxPackets = 512;     // _METER_BUFFER_MAX_PACKETS
    static constexpr double kDrainMinSpacingS = 0.001;// _METER_DRAIN_MIN_SPACING_S

    // ── Release pacing (dejitter) ── [ORION_METER_PACING 2026-08-08]
    // Un-paced, every packet is released at exactly arrival + delay, which
    // copies the flow's ARRIVAL jitter verbatim onto the wire ~delay ms later —
    // the buffer holds hundreds of ms of packets yet smooths nothing. Pacing
    // schedules each release at (previous release + EWMA inter-arrival), so the
    // delivered stream runs at the flow's own average cadence, CLAMPED to
    // nominal ± kPaceMaxShiftS so no packet's effective delay ever deviates
    // from the commanded value by more than that bound (mean delay is preserved
    // because the clamp re-anchors the schedule to arrival truth every packet —
    // drift cannot accumulate). FIFO stays structural (release times remain
    // monotonic). C++-only deviation from the nexus_svc.py reference; the
    // Python debug bridge still releases arrival-shaped.
    static constexpr double kPaceAlpha = 0.05;         // EWMA weight per arrival
    static constexpr double kPaceMinIntervalS = 0.002; // below this the flow is too fast to pace
    static constexpr double kPaceGapResetS = 0.100;    // arrival gap => idle; re-learn cadence
    static constexpr double kPaceMaxShiftS = 0.008;    // |paced - (arrival+delay)| hard bound
    // Servo gain pulling the paced chain toward the nominal (arrival + delay)
    // schedule. Without it the chain's offset random-walks (the EWMA is never
    // exactly the true cadence), eventually rides the +/-kPaceMaxShiftS clamp
    // rail, and while railed the raw arrival jitter passes straight through.
    // With it the offset decays geometrically instead, the clamp almost never
    // binds, and residual release jitter is ~gain * arrival jitter.
    static constexpr double kPaceCorrectionGain = 0.1;

    static constexpr double kFlushFineTickS = 0.0005; // _METER_FLUSH_FINE_TICK_S
    static constexpr double kFlushSlackS = 0.0010;    // _METER_FLUSH_SLACK_S
    static constexpr double kFlushCoarseMaxS = 0.005; // _METER_FLUSH_COARSE_MAX_S
    static constexpr double kFlushIdleS = 0.005;      // _METER_FLUSH_IDLE_S

    static constexpr double kWatchdogTimeoutS = 0.5;  // _METER_WATCHDOG_TIMEOUT_S
    static constexpr double kWatchdogTickS = 0.05;    // _METER_WATCHDOG_TICK_S

    // A captured packet: the raw bytes plus the opaque WINDIVERT_ADDRESS blob,
    // both replayed verbatim on send. Tests construct these with just `data`.
    struct Packet {
        std::vector<std::uint8_t> data;
        std::array<std::uint8_t, 64> addr{};
    };

    struct Snapshot {
        bool active = false;
        double delayMs = 0.0;   // APPLIED right now
        double targetMs = 0.0;  // last accepted TARGET
        bool settled = false;
        int bufferDepth = 0;
    };

    struct Stats {
        std::int64_t intercepted = 0;
        std::int64_t passed = 0;
        std::int64_t delayed = 0;
        std::int64_t sent = 0;
        std::int64_t overflowReleased = 0;
        std::int64_t sendErrors = 0;
        std::int64_t watchdogTrips = 0;
        std::int64_t deadManTrips = 0;
        std::int64_t slewLimitedSteps = 0;
        std::int64_t emergencyZero = 0;
        double peakSlewMsPerS = 0.0;
        double slewCapMsPerS = 0.0;
    };

    using Clock = std::function<double()>;              // monotonic seconds
    using SendSink = std::function<bool(const Packet&)>; // true == sent OK
    using StateBroadcaster = std::function<void(const std::string& state, const std::string& reason)>;
    using LogFn = std::function<void(const std::string& line)>;

    // slewMsPerS<=0 disables the limiter (target and applied converge instantly);
    // used by the ordering/safety tests which drive enqueue() with no flush
    // thread. The shipping path always passes a positive cap.
    explicit MeterDelayIntercept(double slewMsPerS = kMaxSlewMsPerS, Clock clock = {});

    ~MeterDelayIntercept();

    MeterDelayIntercept(const MeterDelayIntercept&) = delete;
    MeterDelayIntercept& operator=(const MeterDelayIntercept&) = delete;

    // Test / wiring seams.
    void setSendSink(SendSink sink);
    void setStateBroadcaster(StateBroadcaster fn) { broadcaster_ = std::move(fn); }
    void setLogger(LogFn fn) { log_ = std::move(fn); }
    void setWinDivert(WinDivertLibrary* lib) { lib_ = lib; }

    // ── Introspection (lock-free where the Python mirror is) ──
    bool active() const { return running_.load(); }
    double currentDelayMs() const { return delayMs_.load(); }
    double targetDelayMs() const { return targetMs_.load(); }
    double peakSlewMsPerS() const { return peakSlewMsPerS_.load(); }
    int bufferDepth() const;
    Snapshot snapshot() const;
    Stats stats() const;
    std::string filter() const;

    // ── Delay control (pure logic) ──
    // Returns the clamped target actually accepted. Throws std::invalid_argument
    // for non-finite input (mapped to "invalid_delay_ms" at the verb layer).
    double setDelay(double delayMs, bool touch = true);
    void forceZero(const std::string& reason);
    double advanceSlew(double now = -1.0);

    // ── Packet path (pure logic) ──
    void enqueue(Packet pkt);
    int flushDue(double now = -1.0);
    // Returns true and sets *out if a packet is queued.
    bool nextReleaseAt(double* out) const;

    // [ORION_METER_PACING 2026-08-08] Release pacing on/off (default ON). Pure
    // logic seam — no wire verb; tests use it to compare paced vs arrival-shaped
    // release timing.
    void setReleasePacing(bool enabled);
    bool releasePacing() const;

    // ── Watchdog (pure logic) ──
    bool watchdogCheck(double now = -1.0);

    // ── Lifecycle (WinDivert handle + threads — service only) ──
    // Returns {ok, errCode}. errCode is "" on success; otherwise a wire-protocol
    // error token ("intercept_requires_ips", "intercept_already_running",
    // "intercept_open_failed: ...").
    std::pair<bool, std::string> start(const std::string& consoleIp, const std::string& courtIp);
    bool stop(const std::string& reason = "client_request");
    bool deadManStop(const std::string& reason);
    void retarget(const std::string& consoleIp, const std::string& courtIp);

    // Build the inbound intercept filter (public for the unit test that pins the
    // filter's clauses — the Remote-Play containment guarantee).
    static std::string buildInterceptFilter(const std::string& consoleIp,
                                            const std::string& courtIp);

    // Validate/canonicalise an IPv4 the same way nexus_svc._validated_ipv4 does.
    // requirePublic=true additionally demands a globally-routable address (used
    // for the court IP — the Remote-Play exclusion). Returns "" on rejection.
    static std::string validatedIpv4(const std::string& value, bool requirePublic = false);

private:
    struct Entry {
        double releaseAt = 0.0;
        double arrivalAt = 0.0;
        Packet pkt;
    };

    double now() const;
    double advanceSlewLocked(double now);       // caller holds mutex_
    void rescheduleLocked(double now, double delayS);
    bool sendLocked(const Packet& pkt);         // caller holds mutex_
    bool winDivertSend(const Packet& pkt);      // real re-inject; caller holds mutex_
    void throttledWarn(const std::string& line);
    void broadcastState(const std::string& state, const std::string& reason);

    void captureLoop();
    void flushLoop();
    void watchdogLoop();

    mutable std::recursive_mutex mutex_;
    Clock clock_;
    SendSink sink_;
    StateBroadcaster broadcaster_;
    LogFn log_;
    WinDivertLibrary* lib_ = nullptr;

    std::deque<Entry> buffer_;
    std::atomic<double> delayMs_{0.0};   // APPLIED
    std::atomic<double> targetMs_{0.0};  // commanded

    // ── Release pacing state (guarded by mutex_) ──
    bool pacingEnabled_ = true;
    double lastArrivalAt_ = -1.0;          // last intercepted arrival (any path)
    double arrivalIntervalEwmaS_ = 0.0;    // 0 = cadence unknown / re-learning
    double lastScheduledReleaseAt_ = -1.0; // pacing anchor across an empty buffer
    double slewMsPerS_ = kMaxSlewMsPerS;
    double lastSlewTs_ = 0.0;
    double lastCmdTs_ = 0.0;
    std::atomic<double> peakSlewMsPerS_{0.0};

    void* handle_ = nullptr;             // WinDivert HANDLE
    void* stopEvent_ = nullptr;          // Win32 event: unblocks the flush timer
    std::atomic<bool> running_{false};
    std::atomic<bool> stopFlag_{true};
    bool starting_ = false;
    std::atomic<int> depth_{0};
    double logThrottleTs_ = 0.0;

    std::string consoleIp_;
    std::string courtIp_;
    std::string filter_;

    std::thread captureThread_;
    std::thread flushThread_;
    std::thread watchdogThread_;

    Stats stats_;
};

} // namespace venicenet
