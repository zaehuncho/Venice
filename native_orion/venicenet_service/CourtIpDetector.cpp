#include "CourtIpDetector.h"

#include <algorithm>
#include <cmath>

namespace venicenet {

std::string CourtIpDetector::endpointKey(const std::string& ip, int port)
{
    return ip + ":" + std::to_string(port);
}

CourtIpDetector::Ipv4Kind CourtIpDetector::ipv4Kind(const std::string& ip)
{
    // Split into exactly four decimal octets, 0-255, no empty parts. Mirrors the
    // Qt classifier's manual parse (which tolerated leading zeros — matched here
    // so detection behaves byte-for-byte identically to the shipped classifier).
    std::uint32_t value = 0;
    int octetCount = 0;
    size_t pos = 0;
    while (true) {
        const size_t dot = ip.find('.', pos);
        const std::string piece =
            (dot == std::string::npos) ? ip.substr(pos) : ip.substr(pos, dot - pos);
        if (piece.empty() || piece.size() > 3) {
            return Ipv4Kind::Invalid;
        }
        int octet = 0;
        for (const char ch : piece) {
            if (ch < '0' || ch > '9') {
                return Ipv4Kind::Invalid;
            }
            octet = octet * 10 + (ch - '0');
        }
        if (octet > 255) {
            return Ipv4Kind::Invalid;
        }
        value = (value << 8U) | static_cast<std::uint32_t>(octet);
        ++octetCount;
        if (octetCount > 4) {
            return Ipv4Kind::Invalid; // "a.b.c.d.e"
        }
        if (dot == std::string::npos) {
            break;
        }
        pos = dot + 1;
    }
    if (octetCount != 4) {
        return Ipv4Kind::Invalid; // too few octets
    }

    const std::uint32_t first = value >> 24U;
    const std::uint32_t second = (value >> 16U) & 0xffU;
    if (first == 10U || (first == 172U && second >= 16U && second <= 31U)
        || (first == 192U && second == 168U)) {
        return Ipv4Kind::PrivateLan;
    }

    const std::uint32_t third = (value >> 8U) & 0xffU;
    const bool nonPublic = first == 0U || first == 127U
        || (first == 100U && second >= 64U && second <= 127U)
        || (first == 169U && second == 254U) || (first == 192U && second == 0U && third == 0U)
        || (first == 192U && second == 0U && third == 2U)
        || (first == 192U && second == 88U && third == 99U)
        || (first == 198U && (second == 18U || second == 19U))
        || (first == 198U && second == 51U && third == 100U)
        || (first == 203U && second == 0U && third == 113U) || first >= 224U;
    return nonPublic ? Ipv4Kind::Other : Ipv4Kind::Public;
}

void CourtIpDetector::trimWindow(Flow& flow, std::int64_t nowMs)
{
    const std::int64_t cutoff = nowMs - kObservationWindowMs;
    size_t removeCount = 0;
    while (removeCount < flow.samples.size() && flow.samples[removeCount].observedMs < cutoff) {
        ++removeCount;
    }
    if (removeCount > 0) {
        flow.samples.erase(flow.samples.begin(), flow.samples.begin() + static_cast<long>(removeCount));
    }
}

double CourtIpDetector::packetRate(const Flow& flow)
{
    if (flow.samples.size() < 2) {
        return 0.0;
    }
    const std::int64_t span = flow.samples.back().observedMs - flow.samples.front().observedMs;
    if (span <= 0) {
        return 0.0;
    }
    return static_cast<double>(flow.samples.size() - 1) * 1000.0 / static_cast<double>(span);
}

bool CourtIpDetector::isQualified(const Flow& flow, std::int64_t nowMs, double rate)
{
    if (static_cast<int>(flow.samples.size()) < kMinimumPackets
        || nowMs - flow.lastObservedMs > kStaleAfterMs) {
        return false;
    }
    const std::int64_t span = flow.samples.back().observedMs - flow.samples.front().observedMs;
    int inbound = 0;
    for (const Sample& s : flow.samples) {
        inbound += s.inbound ? 1 : 0;
    }
    const int outbound = static_cast<int>(flow.samples.size()) - inbound;
    return span >= kMinimumObservationSpanMs && rate >= kMinimumPacketsPerSecond
        && inbound >= kMinimumPacketsPerDirection && outbound >= kMinimumPacketsPerDirection;
}

// [COURT_IP_TRACER 2026-08-08] Names the FIRST failing predicate of
// isQualified(), with the measured numbers, in the same evaluation order.
// Diagnostics only — must stay in lockstep with isQualified().
std::string CourtIpDetector::rejectReason(const Flow& flow, std::int64_t nowMs, double rate)
{
    const int packets = static_cast<int>(flow.samples.size());
    if (packets < kMinimumPackets) {
        return "packets=" + std::to_string(packets) + "<" + std::to_string(kMinimumPackets);
    }
    const std::int64_t staleMs = nowMs - flow.lastObservedMs;
    if (staleMs > kStaleAfterMs) {
        return "stale=" + std::to_string(staleMs) + "ms>" + std::to_string(kStaleAfterMs);
    }
    const std::int64_t span = flow.samples.back().observedMs - flow.samples.front().observedMs;
    if (span < kMinimumObservationSpanMs) {
        return "span=" + std::to_string(span) + "ms<" + std::to_string(kMinimumObservationSpanMs);
    }
    if (rate < kMinimumPacketsPerSecond) {
        return "rate=" + std::to_string(rate).substr(0, std::to_string(rate).find('.') + 3) + "pps<"
            + std::to_string(static_cast<int>(kMinimumPacketsPerSecond));
    }
    int inbound = 0;
    for (const Sample& s : flow.samples) {
        inbound += s.inbound ? 1 : 0;
    }
    const int outbound = packets - inbound;
    if (inbound < kMinimumPacketsPerDirection) {
        return "inbound=" + std::to_string(inbound) + "<"
            + std::to_string(kMinimumPacketsPerDirection);
    }
    if (outbound < kMinimumPacketsPerDirection) {
        return "outbound=" + std::to_string(outbound) + "<"
            + std::to_string(kMinimumPacketsPerDirection);
    }
    return "";
}

bool CourtIpDetector::betterCandidate(const std::string& key, int packets, double rate,
                                      const std::string& incumbentKey, int incumbentPackets,
                                      double incumbentRate)
{
    if (packets != incumbentPackets) {
        return packets > incumbentPackets;
    }
    if (std::abs(rate - incumbentRate) > 0.0001) {
        return rate > incumbentRate;
    }
    return incumbentKey.empty() || key < incumbentKey;
}

void CourtIpDetector::prune(std::int64_t nowMs)
{
    for (auto it = flows_.begin(); it != flows_.end();) {
        if (nowMs - it->second.lastObservedMs > kStaleAfterMs) {
            if (it->first == activeKey_) {
                activeKey_.clear();
            }
            it = flows_.erase(it);
        } else {
            trimWindow(it->second, nowMs);
            ++it;
        }
    }
}

bool CourtIpDetector::observe(const std::string& srcIp, const std::string& dstIp, int srcPort,
                              int dstPort, Transport transport, std::int64_t observedAtMonotonicMs)
{
    if (transport != Transport::Udp || observedAtMonotonicMs < 0 || srcPort <= 0 || srcPort > 65535
        || dstPort <= 0 || dstPort > 65535) {
        return false;
    }
    if (lastClockMs_ >= 0 && observedAtMonotonicMs < lastClockMs_) {
        reset();
        return false;
    }
    lastClockMs_ = observedAtMonotonicMs;
    prune(observedAtMonotonicMs);

    const Ipv4Kind srcKind = ipv4Kind(srcIp);
    const Ipv4Kind dstKind = ipv4Kind(dstIp);
    const bool outbound = srcKind == Ipv4Kind::PrivateLan && dstKind == Ipv4Kind::Public;
    const bool inbound = srcKind == Ipv4Kind::Public && dstKind == Ipv4Kind::PrivateLan;
    if (!outbound && !inbound) {
        return false;
    }

    const std::string publicIp = outbound ? dstIp : srcIp;
    const int publicPort = outbound ? dstPort : srcPort;
    if (publicPort < kCourtPortMin || publicPort > kCourtPortMax) {
        return false;
    }

    const std::string key = endpointKey(publicIp, publicPort);
    auto it = flows_.find(key);
    if (it == flows_.end()) {
        if (static_cast<int>(flows_.size()) >= kMaxTrackedEndpoints) {
            return false;
        }
        Flow flow;
        flow.ip = publicIp;
        flow.port = publicPort;
        flow.lastObservedMs = observedAtMonotonicMs;
        it = flows_.emplace(key, std::move(flow)).first;
    }

    Flow& flow = it->second;
    flow.lastObservedMs = observedAtMonotonicMs;
    if (outbound) {
        ++flow.outboundTotal;
    } else {
        ++flow.inboundTotal;
    }
    flow.samples.push_back({observedAtMonotonicMs, inbound});
    if (static_cast<int>(flow.samples.size()) > kMaxSamplesPerEndpoint) {
        flow.samples.erase(flow.samples.begin(),
                           flow.samples.begin()
                               + (static_cast<long>(flow.samples.size()) - kMaxSamplesPerEndpoint));
    }
    trimWindow(flow, observedAtMonotonicMs);
    return true;
}

CourtIpDetector::Snapshot CourtIpDetector::snapshot(std::int64_t nowMonotonicMs)
{
    if (nowMonotonicMs < 0) {
        return {};
    }
    if (lastClockMs_ >= 0 && nowMonotonicMs < lastClockMs_) {
        reset();
        return {};
    }
    lastClockMs_ = nowMonotonicMs;
    prune(nowMonotonicMs);

    std::string strongestQualified;
    std::string strongestCandidate;
    int strongestQualifiedPackets = -1;
    int strongestCandidatePackets = -1;
    double strongestQualifiedRate = -1.0;
    double strongestCandidateRate = -1.0;
    int qualifiedCount = 0;

    for (auto& entry : flows_) {
        trimWindow(entry.second, nowMonotonicMs);
        const int packets = static_cast<int>(entry.second.samples.size());
        const double rate = packetRate(entry.second);
        const bool qualified = isQualified(entry.second, nowMonotonicMs, rate);
        if (qualified) {
            ++qualifiedCount;
            if (betterCandidate(entry.first, packets, rate, strongestQualified,
                                strongestQualifiedPackets, strongestQualifiedRate)) {
                strongestQualified = entry.first;
                strongestQualifiedPackets = packets;
                strongestQualifiedRate = rate;
            }
        }
        if (betterCandidate(entry.first, packets, rate, strongestCandidate,
                            strongestCandidatePackets, strongestCandidateRate)) {
            strongestCandidate = entry.first;
            strongestCandidatePackets = packets;
            strongestCandidateRate = rate;
        }
    }

    // Retain a still-qualified active endpoint to avoid flicker.
    if (!activeKey_.empty()) {
        auto active = flows_.find(activeKey_);
        if (active != flows_.end()
            && isQualified(active->second, nowMonotonicMs, packetRate(active->second))) {
            strongestQualified = activeKey_;
        }
    }
    activeKey_ = strongestQualified;

    const std::string selectedKey =
        !strongestQualified.empty() ? strongestQualified : strongestCandidate;
    if (selectedKey.empty()) {
        return {};
    }
    auto selectedIt = flows_.find(selectedKey);
    if (selectedIt == flows_.end()) {
        return {};
    }
    const Flow& selected = selectedIt->second;
    Snapshot result;
    result.hasCandidate = true;
    result.qualified = !strongestQualified.empty();
    if (result.qualified) {
        result.endpointIp = selected.ip;
        result.endpointPort = selected.port;
    }
    result.inboundPackets = selected.inboundTotal;
    result.outboundPackets = selected.outboundTotal;
    result.recentPackets = static_cast<int>(selected.samples.size());
    result.packetsPerSecond = packetRate(selected);
    result.ageMs = std::max<std::int64_t>(0, nowMonotonicMs - selected.lastObservedMs);
    result.qualifiedEndpointCount = qualifiedCount;
    // Diagnostics ([COURT_IP_TRACER 2026-08-08]): identify the selected flow and
    // why it is (not) qualified, without touching the predicate above.
    result.candidateIp = selected.ip;
    result.candidatePort = selected.port;
    if (!selected.samples.empty()) {
        result.observationSpanMs =
            selected.samples.back().observedMs - selected.samples.front().observedMs;
        int inbound = 0;
        for (const Sample& s : selected.samples) {
            inbound += s.inbound ? 1 : 0;
        }
        result.recentInbound = inbound;
        result.recentOutbound = static_cast<int>(selected.samples.size()) - inbound;
    }
    if (!result.qualified) {
        result.rejectReason = rejectReason(selected, nowMonotonicMs, result.packetsPerSecond);
        if (result.rejectReason.empty()) {
            // The selected flow passes the predicate but a different (stickier)
            // flow won selection — only possible transiently.
            result.rejectReason = "not_selected";
        }
    }
    return result;
}

void CourtIpDetector::reset()
{
    flows_.clear();
    activeKey_.clear();
    lastClockMs_ = -1;
}

} // namespace venicenet
