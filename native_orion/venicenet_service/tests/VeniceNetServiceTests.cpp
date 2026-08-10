// ───────────────────────────────────────────────────────────────────────────
//  VeniceNetServiceTests — pure-logic coverage for VeniceNetSvc.exe
// ───────────────────────────────────────────────────────────────────────────
//
//  Deliberately NOT a QtTest binary (the service is Qt-free) and NEVER opens
//  WinDivert (kernel driver — won't work in CI). Every test drives the pure port
//  of nexus_svc.py's meter-delay logic through an injectable virtual clock and a
//  recording send sink, plus the protocol parser / builders, the driver_state
//  machine, and the court/Remote-Play IP classification.

#include "CourtIpDetector.h"
#include "IpcServer.h"
#include "Json.h"
#include "MeterDelayIntercept.h"
#include "ServiceArgs.h"
#include "ServiceState.h"

#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

using namespace venicenet;

namespace {

int g_failures = 0;

#define CHECK(cond)                                                                \
    do {                                                                           \
        if (!(cond)) {                                                             \
            ++g_failures;                                                          \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);   \
        }                                                                          \
    } while (0)

// Encode the id in two bytes (big-endian) so ids > 255 survive round-trip — the
// overflow test enqueues 500+ packets.
MeterDelayIntercept::Packet pkt(int id)
{
    MeterDelayIntercept::Packet p;
    p.data.push_back(static_cast<std::uint8_t>((id >> 8) & 0xFF));
    p.data.push_back(static_cast<std::uint8_t>(id & 0xFF));
    return p;
}

int idOf(const MeterDelayIntercept::Packet& p)
{
    if (p.data.size() < 2) {
        return -1;
    }
    return (static_cast<int>(p.data[0]) << 8) | p.data[1];
}

// ── Slew accumulator: cap + token bucket ─────────────────────────────────
void testSlewCapAndTokenBucket()
{
    double vclock = 1000.0;
    MeterDelayIntercept d(MeterDelayIntercept::kMaxSlewMsPerS, [&] { return vclock; });

    // Baseline: converge target==applied==0 and set lastSlewTs to now.
    d.advanceSlew();
    d.setDelay(165.0);

    // A single step of 0.05 s at 100 ms/s can move at most 5 ms.
    vclock += 0.05;
    const double a1 = d.advanceSlew();
    CHECK(std::abs(a1 - 5.0) < 1e-6);

    // Token bucket: a long quiet period must NOT bank an unbounded step. After a
    // 5 s gap, one advance can still only move kMaxSlewMsPerS * kSlewMaxAccumS =
    // 10 ms, not the whole remaining 160.
    vclock += 5.0;
    const double a2 = d.advanceSlew();
    CHECK(std::abs(a2 - (a1 + 10.0)) < 1e-6);

    // The peak applied |D'| never exceeds the cap.
    CHECK(d.peakSlewMsPerS() <= MeterDelayIntercept::kMaxSlewMsPerS + 1e-6);
}

// ── Snap-to-target: the float-residue bug (nexus_svc.py:1082-1095) ────────
void testSnapToTargetReenablesFastPath()
{
    double vclock = 2000.0;
    std::vector<int> sent;
    MeterDelayIntercept d(MeterDelayIntercept::kMaxSlewMsPerS, [&] { return vclock; });
    d.setSendSink([&](const MeterDelayIntercept::Packet& p) {
        sent.push_back(idOf(p));
        return true;
    });

    d.advanceSlew();
    d.setDelay(100.0);
    // Ramp up in fine steps well past the settle point.
    for (int i = 0; i < 60; ++i) {
        vclock += 0.05;
        d.advanceSlew();
    }
    CHECK(d.currentDelayMs() == 100.0); // snapped exactly, not 99.9999...

    // Now ramp back DOWN to 0. Without the snap the applied value lands a hair
    // above 0 forever and the zero-delay fast path never re-fires.
    d.setDelay(0.0);
    for (int i = 0; i < 60; ++i) {
        vclock += 0.05;
        d.advanceSlew();
    }
    CHECK(d.currentDelayMs() == 0.0); // bit-exact zero — the whole point

    // Fast path must re-fire: delay 0 + empty buffer => sent immediately.
    d.enqueue(pkt(7));
    CHECK(sent.size() == 1);
    CHECK(sent.back() == 7);
    CHECK(d.stats().passed >= 1);
    CHECK(d.bufferDepth() == 0);
}

// ── Buffer overflow: early-release OLDEST, never drop, order preserved ─────
void testBufferOverflowReleasesOldestInOrder()
{
    double vclock = 3000.0;
    std::vector<int> sent;
    // Limiter disabled so setDelay applies instantly (ordering/safety-test mode).
    MeterDelayIntercept d(0.0, [&] { return vclock; });
    d.setSendSink([&](const MeterDelayIntercept::Packet& p) {
        sent.push_back(idOf(p));
        return true;
    });

    d.setDelay(1000.0); // clamps to kHardMaxMs (600 since 2026-08-08); nothing flushes at a frozen clock
    CHECK(d.currentDelayMs() == MeterDelayIntercept::kHardMaxMs);

    const int cap = MeterDelayIntercept::kBufferMaxPackets;
    const int total = cap + 3;
    for (int i = 1; i <= total; ++i) {
        d.enqueue(pkt(i));
    }
    // 3 oldest early-released (never dropped); buffer holds exactly cap.
    CHECK(d.bufferDepth() == cap);
    CHECK(d.stats().overflowReleased == 3);
    CHECK(sent.size() == 3);
    CHECK(sent[0] == 1 && sent[1] == 2 && sent[2] == 3); // oldest, in order

    // Drain the rest: advance past the release deadline and flush.
    vclock += 1.0;
    d.flushDue();
    CHECK(static_cast<int>(sent.size()) == total);
    bool ascending = true;
    for (size_t i = 1; i < sent.size(); ++i) {
        if (sent[i] != sent[i - 1] + 1) ascending = false;
    }
    CHECK(ascending); // nothing overtook anything — strict FIFO
    CHECK(sent.front() == 1 && sent.back() == total);
}

// ── Bug A: zero-delay fast path only when the buffer is provably empty ─────
void testFastPathNeverOvertakesBufferedPackets()
{
    double vclock = 4000.0;
    std::vector<int> sent;
    MeterDelayIntercept d(0.0, [&] { return vclock; });
    d.setSendSink([&](const MeterDelayIntercept::Packet& p) {
        sent.push_back(idOf(p));
        return true;
    });

    // delay 0, empty buffer -> fast path.
    d.enqueue(pkt(1));
    CHECK(sent.size() == 1 && sent.back() == 1);

    // Turn the delay on: p2 gets buffered.
    d.setDelay(200.0);
    d.enqueue(pkt(2));
    CHECK(d.bufferDepth() == 1);

    // Turn the delay back to 0 while p2 is still buffered, then enqueue p3.
    // p3 must NOT take the fast path (buffer non-empty), so it can never overtake
    // the older p2.
    d.setDelay(0.0);
    d.enqueue(pkt(3));
    d.flushDue();
    CHECK(sent.size() == 3);
    CHECK(sent[1] == 2 && sent[2] == 3); // arrival order preserved
}

// ── Ordering with a live delay: monotonic release, prefix flush ───────────
void testMonotonicReleaseAndPrefixFlush()
{
    double vclock = 5000.0;
    std::vector<int> sent;
    MeterDelayIntercept d(0.0, [&] { return vclock; });
    d.setSendSink([&](const MeterDelayIntercept::Packet& p) {
        sent.push_back(idOf(p));
        return true;
    });
    d.setDelay(50.0); // 0.05 s

    vclock = 5000.000;
    d.enqueue(pkt(1)); // release ~5000.050
    vclock = 5000.010;
    d.enqueue(pkt(2)); // release max(5000.060, 5000.050) = 5000.060

    d.flushDue(5000.055); // only p1 due
    CHECK(sent.size() == 1 && sent.back() == 1);
    d.flushDue(5000.061); // now p2
    CHECK(sent.size() == 2 && sent.back() == 2);
}

// ── Release pacing: dejitter smooths arrival jitter, bounded around target ─
// [ORION_METER_PACING 2026-08-08] Un-paced, releases copy arrival jitter
// verbatim (release = arrival + delay). Paced, releases run at the flow's EWMA
// cadence, clamped to nominal ± kPaceMaxShiftS. This drives both modes with the
// SAME deterministic jittered 16 ms arrival pattern and asserts: (1) FIFO is
// preserved; (2) inter-release variance drops by more than half; (3) every
// paced packet's effective delay stays within the hard bound of the commanded
// value; (4) the mean delay is preserved (no cumulative drift).
void testReleasePacingSmoothsArrivalJitter()
{
    constexpr double kBaseIntervalS = 0.016; // ~60 Hz flow cadence
    constexpr double kDelayMs = 250.0;
    constexpr int kCount = 120;
    // EWMA warm-up sends excluded from the stats: the first inter-arrival seeds
    // the estimate with its own jitter and the servo needs ~1/kPaceAlpha packets
    // to wash that out. (Offline simulation of this exact deterministic run:
    // raw gap std 8.43 ms -> paced 1.03 ms, max |delay-250| = 8.00 ms, mean
    // delay drift 2.1 ms.)
    constexpr int kWarmup = 30;
    // Deterministic +/-6 ms jitter (inside the +/-8 ms pacing budget; max swing
    // 12 ms < 16 ms keeps arrivals strictly monotonic).
    const double jitter[20] = {0.0,    +0.006, -0.005, +0.004, -0.006,
                               +0.002, -0.003, +0.006, -0.004, 0.0,
                               +0.005, -0.006, +0.003, -0.002, +0.006,
                               -0.005, +0.001, -0.004, +0.006, -0.006};

    struct RunResult {
        std::vector<int> order;
        std::vector<double> sendAt;
        std::vector<double> arriveAt;
    };
    auto run = [&](bool paced) {
        RunResult r;
        double vclock = 9000.0;
        // Limiter disabled: the commanded delay applies instantly, so every
        // packet is measured against one constant target.
        MeterDelayIntercept d(0.0, [&] { return vclock; });
        d.setReleasePacing(paced);
        d.setSendSink([&](const MeterDelayIntercept::Packet& p) {
            r.order.push_back(idOf(p));
            r.sendAt.push_back(vclock);
            return true;
        });
        d.setDelay(kDelayMs);
        std::vector<double> plan(kCount);
        for (int i = 0; i < kCount; ++i) {
            plan[i] = 9000.0 + i * kBaseIntervalS + jitter[i % 20];
        }
        int next = 0;
        const double tEnd = plan.back() + kDelayMs / 1000.0 + 0.1;
        for (double t = 9000.0; t <= tEnd; t += 0.0005) {
            vclock = t;
            while (next < kCount && plan[next] <= t) {
                r.arriveAt.push_back(vclock);
                d.enqueue(pkt(next));
                ++next;
            }
            d.flushDue();
        }
        return r;
    };
    auto stddevOfGaps = [](const RunResult& r) {
        std::vector<double> gaps;
        for (size_t i = kWarmup + 1; i < r.sendAt.size(); ++i) {
            gaps.push_back(r.sendAt[i] - r.sendAt[i - 1]);
        }
        double mean = 0.0;
        for (double g : gaps) mean += g;
        mean /= static_cast<double>(gaps.size());
        double var = 0.0;
        for (double g : gaps) var += (g - mean) * (g - mean);
        var /= static_cast<double>(gaps.size());
        return std::sqrt(var);
    };

    const RunResult raw = run(false);
    const RunResult smooth = run(true);

    // Everything got released, strictly FIFO, in both modes.
    CHECK(static_cast<int>(raw.order.size()) == kCount);
    CHECK(static_cast<int>(smooth.order.size()) == kCount);
    for (int i = 0; i < kCount; ++i) {
        CHECK(raw.order[i] == i);
        CHECK(smooth.order[i] == i);
    }

    // Harness sanity: un-paced per-packet delay is the commanded value (within
    // one 0.5 ms flush step).
    for (int i = 0; i < kCount; ++i) {
        CHECK(std::abs((raw.sendAt[i] - raw.arriveAt[i]) - kDelayMs / 1000.0) < 0.0011);
    }

    // (2) The headline: inter-release jitter collapses.
    const double rawStd = stddevOfGaps(raw);
    const double smoothStd = stddevOfGaps(smooth);
    CHECK(rawStd > 0.003);              // the input jitter really is there
    CHECK(smoothStd < 0.5 * rawStd);    // required: variance drops by > half
    CHECK(smoothStd < 0.002);           // and is small in absolute terms

    // (3) Hard bound: no paced packet deviates from the commanded delay by more
    // than kPaceMaxShiftS (+ one flush step of measurement slack).
    for (size_t i = kWarmup; i < smooth.sendAt.size(); ++i) {
        const double d_i = smooth.sendAt[i] - smooth.arriveAt[i];
        CHECK(std::abs(d_i - kDelayMs / 1000.0)
              <= MeterDelayIntercept::kPaceMaxShiftS + 0.0011);
    }

    // (4) Mean delay preserved: pacing redistributes within the bound, it does
    // not add or shed latency in expectation.
    double rawMean = 0.0;
    double smoothMean = 0.0;
    for (int i = kWarmup; i < kCount; ++i) {
        rawMean += raw.sendAt[i] - raw.arriveAt[i];
        smoothMean += smooth.sendAt[i] - smooth.arriveAt[i];
    }
    rawMean /= static_cast<double>(kCount - kWarmup);
    smoothMean /= static_cast<double>(kCount - kWarmup);
    CHECK(std::abs(smoothMean - rawMean) < 0.004);
}

// ── setDelay clamp + invalid ──────────────────────────────────────────────
void testSetDelayClampAndInvalid()
{
    MeterDelayIntercept d(0.0, [] { return 6000.0; });
    CHECK(d.setDelay(1000.0) == MeterDelayIntercept::kHardMaxMs); // clamp high
    CHECK(d.setDelay(-25.0) == 0.0);                              // clamp low
    CHECK(d.setDelay(165.0) == 165.0);                           // pass-through
    bool threw = false;
    try {
        d.setDelay(std::nan(""));
    } catch (...) {
        threw = true;
    }
    CHECK(threw);
}

// ── watchdog: standing non-zero delay forced to zero on command starvation ─
void testWatchdogForcesZeroOnStarvation()
{
    double vclock = 7000.0;
    MeterDelayIntercept d(0.0, [&] { return vclock; });
    // The watchdog only fires while running_; simulate that by driving the pure
    // path — running_ is false without start(), so watchdogCheck returns false.
    // Instead assert force_zero re-pacing and the emergency-zero stat directly.
    d.setDelay(150.0);
    CHECK(d.currentDelayMs() == 150.0);
    d.forceZero("test");
    CHECK(d.currentDelayMs() == 0.0);
    CHECK(d.targetDelayMs() == 0.0);
    CHECK(d.stats().emergencyZero == 1);
}

// ── Remote-Play IP exclusion + court/console validation ───────────────────
void testIpValidationExclusion()
{
    // Console IP may be private (that is the point). Court IP must be public.
    CHECK(MeterDelayIntercept::validatedIpv4("192.168.137.100") == "192.168.137.100");
    CHECK(MeterDelayIntercept::validatedIpv4("192.168.137.100", true).empty()); // LAN not public
    CHECK(MeterDelayIntercept::validatedIpv4("127.0.0.1").empty());             // loopback
    CHECK(MeterDelayIntercept::validatedIpv4("0.0.0.0").empty());               // unspecified
    CHECK(MeterDelayIntercept::validatedIpv4("224.0.0.1").empty());             // multicast
    CHECK(MeterDelayIntercept::validatedIpv4("8.8.8.8", true) == "8.8.8.8");    // public OK
    CHECK(MeterDelayIntercept::validatedIpv4("10.0.0.5", true).empty());        // private
    CHECK(MeterDelayIntercept::validatedIpv4("999.1.1.1").empty());             // syntactic
    CHECK(MeterDelayIntercept::validatedIpv4("1.2.3").empty());                 // too few

    // The intercept filter pins court as SrcAddr and console as DstAddr and the
    // 30000-30020 port range — the clauses that keep Remote Play (LAN<->LAN) out.
    const std::string f = MeterDelayIntercept::buildInterceptFilter("192.168.1.50", "8.8.8.8");
    CHECK(f.find("ip.SrcAddr == 8.8.8.8") != std::string::npos);
    CHECK(f.find("ip.DstAddr == 192.168.1.50") != std::string::npos);
    CHECK(f.find("udp.SrcPort >= 30000") != std::string::npos);
    CHECK(f.find("udp.DstPort <= 30020") != std::string::npos);
}

// ── Court detector: qualifies a public flow, rejects LAN<->LAN ─────────────
void testCourtDetection()
{
    CourtIpDetector det;
    std::int64_t t = 0;
    // A private<->public UDP flow on a court port, both directions, sustained.
    for (int i = 0; i < 20; ++i) {
        t += 40;
        // alternate direction
        if (i % 2 == 0) {
            det.observe("192.168.1.50", "8.8.8.8", 5000, 30005, CourtIpDetector::Transport::Udp, t);
        } else {
            det.observe("8.8.8.8", "192.168.1.50", 30005, 5000, CourtIpDetector::Transport::Udp, t);
        }
    }
    const auto snap = det.snapshot(t);
    CHECK(snap.qualified);
    CHECK(snap.endpointIp == "8.8.8.8");
    CHECK(snap.endpointPort == 30005);

    // LAN<->LAN (Remote Play shape) must never qualify.
    CourtIpDetector rp;
    std::int64_t t2 = 0;
    for (int i = 0; i < 20; ++i) {
        t2 += 40;
        rp.observe("192.168.1.50", "192.168.1.60", 5000, 30005, CourtIpDetector::Transport::Udp,
                   t2);
    }
    CHECK(!rp.snapshot(t2).qualified);
}

// ── driver_state machine + armed preconditions ─────────────────────────────
void testStateMachine()
{
    ServiceStateMachine sm;
    CHECK(sm.driverState() == DriverState::NotInstalled);
    CHECK(!sm.armed());

    // NOT_INSTALLED -> HANDLE_OPEN directly is illegal (docs: no skipping probe).
    CHECK(!sm.onHandleOpened());
    CHECK(sm.driverState() == DriverState::NotInstalled);

    CHECK(sm.onProbe(true, false));
    CHECK(sm.driverState() == DriverState::NotStarted);
    CHECK(sm.onProbe(true, true));
    CHECK(sm.driverState() == DriverState::Ready);

    // Arming requires HANDLE_OPEN, so it is refused in READY.
    CHECK(!sm.setArmed(true, true, true));
    CHECK(!sm.armed());

    CHECK(sm.onHandleOpened());
    CHECK(sm.driverState() == DriverState::HandleOpen);

    // Arming requires enabled && court detected too.
    CHECK(!sm.setArmed(true, false, true));
    CHECK(!sm.setArmed(true, true, false));
    CHECK(sm.setArmed(true, true, true));
    CHECK(sm.armed());

    // Error disarms and is sticky until a probe recovers it.
    CHECK(sm.onError(ServiceError::DriverOpenFailed, "open denied"));
    CHECK(sm.driverState() == DriverState::Error);
    CHECK(!sm.armed());
    CHECK(sm.errorCode() == ServiceError::DriverOpenFailed);
    CHECK(sm.errorText() == "open denied");
    CHECK(sm.onProbe(true, true)); // recovery
    CHECK(sm.driverState() == DriverState::Ready);
    CHECK(sm.errorCode() == ServiceError::Ok);

    // HANDLE_OPEN -> READY on close.
    CHECK(sm.onHandleOpened());
    CHECK(sm.onHandleClosed());
    CHECK(sm.driverState() == DriverState::Ready);
}

// ── Protocol parsing / builders (byte-compatible with nexus_svc.py) ────────
void testProtocolParsingAndBuilders()
{
    // JSON round trip + verb extraction (case-insensitive, like nexus_svc).
    CHECK(IpcServer::extractCmdVerb("{\"cmd\":\"Set_Meter_Delay\",\"delay_ms\":165}")
          == "set_meter_delay");
    CHECK(IpcServer::extractCmdVerb("{\"cmd\":\"auth\",\"token\":\"abc\"}") == "auth");
    CHECK(IpcServer::extractCmdVerb("not json").empty());

    // Number formatting matches Python's round(x,3): integers print without ".0".
    CHECK(JsonValue(165.0).serialize() == "165");
    CHECK(JsonValue(164.5).serialize() == "164.5");
    CHECK(JsonValue(static_cast<std::int64_t>(3)).serialize() == "3");

    // hello advertises meter_delay only when armed.
    JsonValue hello;
    CHECK(JsonValue::parse(IpcServer::buildHelloLine(kSvcVersion, {"meter_delay"}), hello));
    CHECK(hello.getString("event") == "hello");
    CHECK(hello.getNumber("version") == kSvcVersion);
    const JsonValue* feats = hello.find("features");
    CHECK(feats && feats->isArray() && feats->elements().size() == 1);
    CHECK(feats && feats->elements()[0].asString() == "meter_delay");

    JsonValue emptyFeatures;
    CHECK(JsonValue::parse(IpcServer::buildHelloLine(kSvcVersion, {}), emptyFeatures));
    const JsonValue* ef = emptyFeatures.find("features");
    CHECK(ef && ef->isArray() && ef->elements().empty());

    // unauthorized carries reason + token_path + token_mode.
    JsonValue unauth;
    CHECK(JsonValue::parse(
        IpcServer::buildUnauthorizedLine("token_mismatch", "C:/pd/nexus_bridge.token", "service"),
        unauth));
    CHECK(unauth.getString("msg") == "unauthorized");
    CHECK(unauth.getString("reason") == "token_mismatch");
    CHECK(unauth.getString("token_path") == "C:/pd/nexus_bridge.token");
    CHECK(unauth.getString("token_mode") == "service");

    // disarmed loud-path.
    JsonValue disarmed;
    CHECK(JsonValue::parse(IpcServer::buildDisarmedErrorLine("set_meter_delay"), disarmed));
    CHECK(disarmed.getString("msg") == "meter_delay_disarmed");
    CHECK(disarmed.getString("cmd") == "set_meter_delay");

    // unknown verb error.
    JsonValue unknown;
    CHECK(JsonValue::parse(IpcServer::buildUnknownCommandLine("frobnicate"), unknown));
    CHECK(unknown.getString("msg") == "unknown_command");
    CHECK(unknown.getString("cmd") == "frobnicate");

    // constant-time token compare.
    CHECK(IpcServer::constantTimeEquals("deadbeef", "deadbeef"));
    CHECK(!IpcServer::constantTimeEquals("deadbeef", "deadbeee"));
    CHECK(!IpcServer::constantTimeEquals("deadbeef", "dead"));

    // snapshot fields carry the applied-vs-target split the client keys on.
    MeterDelayIntercept::Snapshot snap;
    snap.active = true;
    snap.delayMs = 82.5;
    snap.targetMs = 165.0;
    snap.settled = false;
    snap.bufferDepth = 12;
    JsonValue obj = JsonValue::makeObject();
    IpcServer::addSnapshotFields(obj, snap);
    JsonValue reparsed;
    CHECK(JsonValue::parse(obj.serialize(), reparsed));
    CHECK(reparsed.getBool("meter_delay_active"));
    CHECK(reparsed.getNumber("meter_delay_ms") == 82.5);
    CHECK(reparsed.getNumber("meter_delay_target_ms") == 165.0);
    CHECK(!reparsed.getBool("meter_delay_settled"));
    CHECK(reparsed.getNumber("meter_buffer_depth") == 12);
}

// ── Arg routing (ports _meter_arm_requested / _is_service_run_invocation) ──
void testArgRouting()
{
    CHECK(isServiceRunInvocation({}));
    CHECK(isServiceRunInvocation({"--arm-meter-delay"}));
    CHECK(!isServiceRunInvocation({"install"}));
    CHECK(!isServiceRunInvocation({"debug"}));

    CHECK(meterArmRequested({"--arm-meter-delay"}, "").first);
    CHECK(meterArmRequested({"--arm-meter-delay"}, "").second == "cli");
    CHECK(meterArmRequested({}, "1").first);
    CHECK(meterArmRequested({}, "on").second == "env");
    CHECK(meterArmRequested({}, "TRUE").first);
    CHECK(!meterArmRequested({}, "0").first);
    CHECK(!meterArmRequested({}, "").first);
}

// The IPC line cap is 16 KiB and IpcServer parses EVERY line before the auth check,
// so an unauthenticated peer on 127.0.0.1 controls this parser's recursion depth
// directly. Without a cap, "[[[[..." x16000 overflows the 1 MB thread stack and kills a
// LocalSystem service. Reject deep nesting; keep accepting real protocol documents.
void testJsonParserDepthLimit()
{
    // Legitimate protocol shapes still parse. The real ones nest 2-3 deep.
    JsonValue ok;
    CHECK(JsonValue::parse("{\"cmd\":\"set_meter_delay\",\"args\":{\"ms\":200}}", ok));
    CHECK(ok.isObject());
    CHECK(ok.getString("cmd") == "set_meter_delay");
    CHECK(ok.has("args"));
    JsonValue nested;
    CHECK(JsonValue::parse("{\"a\":{\"b\":{\"c\":[1,2,{\"d\":true}]}}}", nested));
    CHECK(nested.isObject());
    CHECK(nested.has("a"));

    // Right at the cap: 32 nested arrays parse, 33 do not. Asserting both sides
    // pins the boundary so a later refactor cannot quietly drop the guard by
    // making the limit enormous.
    {
        JsonValue atCap;
        CHECK(JsonValue::parse(std::string(32, '[') + std::string(32, ']'), atCap));
        JsonValue overCap;
        CHECK(!JsonValue::parse(std::string(33, '[') + std::string(33, ']'), overCap));
    }

    // The attack itself, at the real 16 KiB line cap. Must return false, not crash.
    // Unterminated on purpose: a hostile peer has no reason to close its brackets.
    {
        JsonValue bomb;
        CHECK(!JsonValue::parse(std::string(16000, '['), bomb));
        CHECK(bomb.isNull());
    }
    {
        JsonValue bomb;
        CHECK(!JsonValue::parse(std::string(8000, '{'), bomb));
    }
    // Balanced, so only the depth guard can reject it.
    {
        JsonValue bomb;
        CHECK(!JsonValue::parse(std::string(8000, '[') + std::string(8000, ']'), bomb));
    }
}

} // namespace

int main()
{
    testJsonParserDepthLimit();
    testSlewCapAndTokenBucket();
    testSnapToTargetReenablesFastPath();
    testBufferOverflowReleasesOldestInOrder();
    testFastPathNeverOvertakesBufferedPackets();
    testMonotonicReleaseAndPrefixFlush();
    testReleasePacingSmoothsArrivalJitter();
    testSetDelayClampAndInvalid();
    testWatchdogForcesZeroOnStarvation();
    testIpValidationExclusion();
    testCourtDetection();
    testStateMachine();
    testProtocolParsingAndBuilders();
    testArgRouting();

    if (g_failures != 0) {
        std::fprintf(stderr, "VeniceNetServiceTests: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("VeniceNetServiceTests: all checks passed\n");
    return 0;
}
