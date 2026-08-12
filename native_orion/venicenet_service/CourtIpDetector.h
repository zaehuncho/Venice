#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  CourtIpDetector.h — passive court-server flow identification (Qt-free port)
// ───────────────────────────────────────────────────────────────────────────
//
//  A faithful, Qt-free port of native_orion/src/PassiveCourtFlowClassifier.h.
//  In the nexus_svc.py design the CLIENT ran this logic over the `packet` event
//  stream and told the service the court IP via `set_court_ip`. Under Option 3
//  (SYSTEM service in C++) the SERVICE now owns detection as well (per
//  docs/VENICENET_API.md: "the DLL owns court-IP detection" — and the DLL is now
//  a thin client, so the service is the real owner). The service still:
//    * accepts an explicit `set_court_ip` (client override — wire compatible),
//    * broadcasts `packet` events so the existing NetworkBridge client's own
//      classifier keeps working unchanged (wire compatible).
//  The detector feeds the intercept's court IP when the client has not pinned
//  one, and only ever qualifies a PUBLIC endpoint on the court port range, which
//  is what structurally keeps Remote Play (LAN<->LAN) out of the intercept.
//
//  Semantics are kept identical to the Qt version: same constants, same IPv4
//  kind classification, same qualification predicate, same "strongest candidate"
//  selection with active-key stickiness. It is deliberately unit-testable with
//  no socket and a caller-supplied monotonic clock.

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

namespace venicenet {

class CourtIpDetector {
public:
    enum class Transport { Udp, Tcp, Other };

    static constexpr int kCourtPortMin = 30000;
    static constexpr int kCourtPortMax = 30099;
    // [ORION_PORT_RANGE_MISMATCH 2026-08-11] This detector qualifies 30000-30099, but
    // MeterDelayIntercept's WinDivert filter only matches 30000-30020 (MeterDelayIntercept.h
    // kPortMax / the filter string in MeterDelayIntercept.cpp). A real court flow on 30021-30099
    // therefore QUALIFIES here, the service reports active=true, and the filter matches ZERO
    // packets -- meter delay silently does nothing while claiming to work. That can burn an entire
    // tuning session, and it is invisible without reading two headers side by side.
    //
    // DELIBERATELY NOT "FIXED" BY MOVING EITHER BOUND. Narrowing this to 30020 would make a
    // legitimate high-port flow undetectable (delay dies with no court at all); widening the
    // WinDivert filter changes what a LocalSystem kernel driver captures and needs real NBA 2K26
    // port evidence we do not have. Both silently trade one failure for another. Instead
    // portOutsideInterceptRange() lets the caller SAY SO -- a loud, honest "detected but cannot
    // intercept" beats a quiet lie, and it is the only version that is safe 17 days from ship.
    // Revisit with real port evidence; see METER_DELAY_PLAN_2026-08-12.md Option 5.
    static constexpr int kInterceptPortMax = 30020;
    [[nodiscard]] static constexpr bool portOutsideInterceptRange(int port) noexcept
    {
        return port > kInterceptPortMax && port <= kCourtPortMax;
    }
    static constexpr int kMinimumPackets = 12;
    static constexpr int kMinimumPacketsPerDirection = 3;
    static constexpr std::int64_t kMinimumObservationSpanMs = 400;
    static constexpr double kMinimumPacketsPerSecond = 10.0;
    static constexpr std::int64_t kObservationWindowMs = 1500;
    static constexpr std::int64_t kStaleAfterMs = 2000;
    static constexpr int kMaxTrackedEndpoints = 16;
    static constexpr int kMaxSamplesPerEndpoint = 512;

    struct Snapshot {
        bool hasCandidate = false;
        bool qualified = false;
        std::string endpointIp;
        int endpointPort = 0;
        std::uint64_t inboundPackets = 0;
        std::uint64_t outboundPackets = 0;
        int recentPackets = 0;
        double packetsPerSecond = 0.0;
        std::int64_t ageMs = 0;
        int qualifiedEndpointCount = 0;
        // [COURT_IP_TRACER 2026-08-08] Diagnostics-only fields; the qualification
        // predicate itself is untouched. candidateIp/Port are filled for the
        // selected flow even when it is NOT qualified (endpointIp keeps its
        // qualified-only contract). rejectReason names the FIRST failing
        // predicate, with numbers, in isQualified()'s evaluation order.
        std::string candidateIp;
        int candidatePort = 0;
        std::int64_t observationSpanMs = 0;
        int recentInbound = 0;
        int recentOutbound = 0;
        std::string rejectReason;
    };

    // Returns true only when the observation belongs to a syntactically valid
    // private-LAN <-> public-IPv4 UDP flow whose PUBLIC port is in range.
    bool observe(const std::string& srcIp, const std::string& dstIp, int srcPort, int dstPort,
                 Transport transport, std::int64_t observedAtMonotonicMs);

    Snapshot snapshot(std::int64_t nowMonotonicMs);

    void reset();

private:
    enum class Ipv4Kind { Invalid, PrivateLan, Public, Other };

    struct Sample {
        std::int64_t observedMs = 0;
        bool inbound = false;
    };
    struct Flow {
        std::string ip;
        int port = 0;
        std::uint64_t inboundTotal = 0;
        std::uint64_t outboundTotal = 0;
        std::int64_t lastObservedMs = 0;
        std::vector<Sample> samples;
    };

    static std::string endpointKey(const std::string& ip, int port);
    static Ipv4Kind ipv4Kind(const std::string& ip);
    static void trimWindow(Flow& flow, std::int64_t nowMs);
    static double packetRate(const Flow& flow);
    static bool isQualified(const Flow& flow, std::int64_t nowMs, double rate);
    static std::string rejectReason(const Flow& flow, std::int64_t nowMs, double rate);
    static bool betterCandidate(const std::string& key, int packets, double rate,
                                const std::string& incumbentKey, int incumbentPackets,
                                double incumbentRate);
    void prune(std::int64_t nowMs);

    std::unordered_map<std::string, Flow> flows_;
    std::string activeKey_;
    std::int64_t lastClockMs_ = -1;
};

} // namespace venicenet
